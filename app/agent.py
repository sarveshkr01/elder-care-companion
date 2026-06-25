import datetime
import json
import os
import re
from typing import Any

from pydantic import BaseModel, Field
from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.models import Gemini
from google.adk import Context, Workflow
from google.adk.workflow import Edge, FunctionNode, START
from google.adk.tools import AgentTool, McpToolset
from google.adk.events import RequestInput, Event
from google.genai import types
from mcp import StdioServerParameters
from google.adk.tools.mcp_tool import StdioConnectionParams

from app.config import config

# 1. State Schema definition
class ElderCareState(BaseModel):
    query: str = ""
    pii_redacted_query: str = ""
    appointment_details: str = ""
    medication_details: str = ""
    doctor_visit_status: str = ""
    medication_status: str = ""
    caregiver_alerted: bool = False

# 2. Local Tools for Agents
def log_medication(ctx: Context, medication_name: str, status: str) -> str:
    """Logs the medication status in the senior's medical log.
    
    Args:
        medication_name: The name of the medication (e.g. 'Aspirin').
        status: The status, e.g. 'taken', 'missed'.
    """
    ctx.state['medication_details'] = f"{medication_name} - {status}"
    ctx.state['medication_status'] = "LOGGED"
    return f"Medication log updated: {medication_name} is marked as {status}."

def draft_appointment(ctx: Context, doctor_name: str, appointment_time: str) -> str:
    """Drafts a doctor visit appointment request for approval.
    
    Args:
        doctor_name: The name of the doctor.
        appointment_time: The requested time/day for the visit.
    """
    ctx.state['appointment_details'] = f"Visit with {doctor_name} at {appointment_time}"
    ctx.state['doctor_visit_status'] = "PENDING_APPROVAL"
    return f"Drafted appointment request: Visit with {doctor_name} at {appointment_time}. Caregiver approval is required."

# Initialize Model
model = Gemini(
    model=config.model,
    retry_options=types.HttpRetryOptions(attempts=3),
)

# Define MCP Toolset (StdioConnectionParams is the recommended API)
mcp_toolset = McpToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command="uv",
            args=["run", "python", "-m", "app.mcp_server"],
        ),
        timeout=30.0,
    )
)

# 3. Sub-agents definition
medication_agent = Agent(
    name="medication_agent",
    model=model,
    instruction="""You are the Medication Assistant. You help log medication status, check schedules, and verify availability.
    Use the `log_medication` tool to record when a medicine is taken or missed.
    Use the `check_medication_availability` MCP tool to check the stock levels of drugs in the home.
    Use the `get_caregiver_contact` MCP tool if you need to alert the caregiver.
    Respond with a summary of the log or status check.""",
    tools=[log_medication, mcp_toolset],
)

appointment_agent = Agent(
    name="appointment_agent",
    model=model,
    instruction="""You are the Appointment Assistant. You help schedule doctor visits.
    Use the `draft_appointment` tool to create a draft request for a doctor visit.
    Use the `get_doctor_schedules` MCP tool to check doctor availability before drafting.
    Use the `get_caregiver_contact` MCP tool to confirm primary contact details.
    Explain that a caregiver needs to approve it before it is confirmed.""",
    tools=[draft_appointment, mcp_toolset],
)

orchestrator_agent = Agent(
    name="orchestrator_agent",
    model=model,
    instruction="""You are the Elder Care Orchestrator. Help the senior manage medication schedules or schedule doctor appointments.
    You have two specialized tools:
    - `medication_agent`: For any queries about taking, logging, or reviewing medication.
    - `appointment_agent`: For any queries about scheduling, checking, or modifying doctor visits.
    Always delegate to these specialized tools. If a doctor appointment is drafted, inform the user that caregiver approval has been requested.""",
    tools=[AgentTool(medication_agent), AgentTool(appointment_agent)],
)

# 4. Workflow Function Nodes
def security_checkpoint(ctx: Context, node_input: str) -> Event:
    """Checks for prompt injection, scrubs PII, and logs audit events."""
    log_entry = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "event_type": "security_scan",
        "severity": "INFO",
        "details": {}
    }
    
    # Simple check for prompt injection
    injection_keywords = ["ignore instructions", "system prompt", "override instructions", "jailbreak", "ignore previous"]
    detected_injection = False
    for kw in injection_keywords:
        if kw in node_input.lower():
            detected_injection = True
            break
            
    if detected_injection:
        log_entry["severity"] = "CRITICAL"
        log_entry["details"]["error"] = "Prompt injection detected"
        log_entry["details"]["input_snippet"] = node_input[:100]
        print(json.dumps(log_entry))
        return Event(route="SECURITY_EVENT", output="Security Block: Prompt injection detected.")
        
    # PII Scrubbing
    scrubbed = node_input
    scrubbed = re.sub(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', '[EMAIL_REDACTED]', scrubbed)
    scrubbed = re.sub(r'\b\d{3}[-.]?\d{3}[-.]?\d{4}\b', '[PHONE_REDACTED]', scrubbed)
    scrubbed = re.sub(r'\b\d{3}-\d{2}-\d{4}\b', '[SSN_REDACTED]', scrubbed)
    
    # Domain Rule
    if "bypass consent" in node_input.lower():
        log_entry["severity"] = "WARNING"
        log_entry["details"]["warning"] = "Consent bypass attempt detected"
        print(json.dumps(log_entry))
        return Event(route="SECURITY_EVENT", output="Security Block: Consent bypass is not permitted.")
        
    log_entry["details"]["pii_redacted"] = (scrubbed != node_input)
    print(json.dumps(log_entry))
    
    ctx.state['query'] = node_input
    ctx.state['pii_redacted_query'] = scrubbed
    return Event(route="SAFE")

def security_alert(ctx: Context, node_input: str) -> str:
    """Handles security violation events by notifying and logging."""
    return f"Access Denied: The request was blocked by the security scanner. Reason: {node_input}"

async def run_orchestrator(ctx: Context, pii_redacted_query: str) -> str:
    """Invokes the orchestrator agent dynamically."""
    result = await ctx.run_node(orchestrator_agent, node_input=pii_redacted_query)
    return result

def route_orchestrator_output(ctx: Context) -> Event:
    """Decides if caregiver approval is required based on agent decision."""
    if ctx.state.get('doctor_visit_status') == 'PENDING_APPROVAL':
        return Event(route='NEEDS_APPROVAL')
    return Event(route='COMPLETE')

def appointment_approval(ctx: Context) -> RequestInput | str:
    """Requests approval from caregiver for doctor visit."""
    # Note: when workflow resumes, the return value of this node will be bypassed
    # and replaced with the user's response because rerun_on_resume is False by default.
    return RequestInput(
        message=f"Caregiver approval required for scheduling appointment: {ctx.state.get('appointment_details')}. Please respond with 'approve' or 'deny'.",
        response_schema=str
    )

def process_approval(ctx: Context, node_input: str) -> str:
    """Processes approval or denial response from caregiver."""
    if 'approve' in node_input.lower() or 'yes' in node_input.lower():
        ctx.state['doctor_visit_status'] = 'APPROVED'
        return "Doctor visit appointment approved and scheduled."
    else:
        ctx.state['doctor_visit_status'] = 'DENIED'
        return "Doctor visit appointment request denied."

def final_output(ctx: Context) -> str:
    """Compiles the final system response."""
    status_msg = ""
    if ctx.state.get('doctor_visit_status') == 'APPROVED':
        status_msg += f"Doctor visit confirmed: {ctx.state.get('appointment_details')}. "
    elif ctx.state.get('doctor_visit_status') == 'DENIED':
        status_msg += "Doctor visit request was denied by the caregiver. "
        
    if ctx.state.get('medication_status') == 'LOGGED':
        status_msg += f"Medication log updated: {ctx.state.get('medication_details')}."
        
    return status_msg or "Request processed successfully."

# 5. Define Workflow Nodes using FunctionNode
security_checkpoint_node = FunctionNode(func=security_checkpoint, name="security_checkpoint_node", state_schema=ElderCareState)
security_alert_node = FunctionNode(func=security_alert, name="security_alert_node", state_schema=ElderCareState)
# rerun_on_resume=True is required here: this node uses ctx.run_node() to dynamically
# schedule orchestrator_agent, and the workflow must re-run this node after any child
# interrupt to collect the child's response.
orchestrator_node = FunctionNode(func=run_orchestrator, name="orchestrator_node", state_schema=ElderCareState, rerun_on_resume=True)
route_orchestrator_node = FunctionNode(func=route_orchestrator_output, name="route_orchestrator_node", state_schema=ElderCareState)
appointment_approval_node = FunctionNode(func=appointment_approval, name="appointment_approval_node", state_schema=ElderCareState)
process_approval_node = FunctionNode(func=process_approval, name="process_approval_node", state_schema=ElderCareState)
final_output_node = FunctionNode(func=final_output, name="final_output_node", state_schema=ElderCareState)

# 6. Define Workflow Graph and Edges
workflow_edges = [
    Edge(from_node=START, to_node=security_checkpoint_node),
    Edge(from_node=security_checkpoint_node, to_node=security_alert_node, route="SECURITY_EVENT"),
    Edge(from_node=security_checkpoint_node, to_node=orchestrator_node, route="SAFE"),
    Edge(from_node=orchestrator_node, to_node=route_orchestrator_node),
    Edge(from_node=route_orchestrator_node, to_node=appointment_approval_node, route="NEEDS_APPROVAL"),
    Edge(from_node=route_orchestrator_node, to_node=final_output_node, route="COMPLETE"),
    Edge(from_node=appointment_approval_node, to_node=process_approval_node),
    Edge(from_node=process_approval_node, to_node=final_output_node),
]

elder_care_workflow = Workflow(
    name="elder_care_workflow",
    edges=workflow_edges,
    state_schema=ElderCareState,
)

# 7. Create ADK App
app = App(
    root_agent=elder_care_workflow,
    name="app",
)
