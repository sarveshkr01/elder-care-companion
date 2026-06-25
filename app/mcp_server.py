from mcp.server.fastmcp import FastMCP

mcp = FastMCP("elder-care-mcp")

@mcp.tool()
def get_caregiver_contact() -> str:
    """Get caregiver primary contact details for alerts."""
    return "Primary Caregiver: Sarah Doe, Phone: 555-0199, Email: sarah.doe@example.com"

@mcp.tool()
def check_medication_availability(medication_name: str) -> str:
    """Check availability of a medication in the household.
    
    Args:
        medication_name: Name of the medication to check.
    """
    inventory = {
        "aspirin": "In stock (45 pills remaining).",
        "metformin": "Low stock (5 pills remaining) - refill recommended.",
        "lisinopril": "Out of stock - please reorder."
    }
    return inventory.get(medication_name.lower(), f"{medication_name} status: Unknown stock level. Please check physical cabinet.")

@mcp.tool()
def get_doctor_schedules(doctor_name: str) -> str:
    """Get the available schedule slots for a doctor.
    
    Args:
        doctor_name: Name of the doctor.
    """
    schedules = {
        "dr. smith": "Available slots: Monday 10:00 AM, Wednesday 2:00 PM",
        "dr. jones": "Available slots: Tuesday 1:00 PM, Thursday 9:00 AM",
        "dr. lee": "Available slots: Friday 11:00 AM, Friday 3:00 PM"
    }
    return schedules.get(doctor_name.lower(), f"No specific schedule for {doctor_name}. Recommended: Call reception at 555-0100.")

if __name__ == "__main__":
    mcp.run()
