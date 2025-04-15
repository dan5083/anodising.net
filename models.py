import os
import json
import requests
import logging
from collections import defaultdict
import math
from datetime import datetime, timedelta
from azure.storage.blob import BlobServiceClient
from azure.core.exceptions import AzureError
from tenacity import retry, stop_after_attempt, wait_exponential
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import CheckConstraint, DECIMAL, Column, Integer, Float, JSON, ForeignKey, String, func
from sqlalchemy.orm import relationship, aliased, validates
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.ext.hybrid import hybrid_property

# Initialize SQLAlchemy
db = SQLAlchemy()

# Configure the logger
logging.basicConfig(
    level=logging.INFO, #  Adjust as needed (DEBUG, INFO, WARNING, ERROR,  CRITICAL)
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

# Create a named logger for the application
logger = logging.getLogger("azureapp")

# Remove any default handlers to avoid duplicates
if logger.hasHandlers():
    logger.handlers.clear()

# Add a console handler for simplicity
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)  # Set log level for console
formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
console_handler.setFormatter(formatter)

class User(db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(255), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)

class Jig(db.Model):
    __tablename__ = 'jigs_inventory'
    __table_args__ = (
        db.UniqueConstraint('jig_type', name='uq_jig_type'),
        {'extend_existing': True}
    )

    jig_id = db.Column(db.Integer, primary_key=True)
    jig_type = db.Column(db.String(100), nullable=False)
    gross_stock = db.Column(db.Integer, nullable=False, default=1)
    maxUPJ = db.Column(db.Integer, nullable=False)
    maxJPL = db.Column(db.Integer, nullable=False)
    MPJ = db.Column(db.Integer, nullable=False)
    image = db.Column(db.String(255), nullable=True)

class Customer(db.Model):
    __tablename__ = 'customers'
    __table_args__ = {'extend_existing': True}

    customer_id = db.Column(db.Integer, primary_key=True)
    customer_name = db.Column(db.String(255), nullable=False)
    contact_info = db.Column(db.String(255))

    #  Relationships
    component_jobs = db.relationship('ComponentJob', back_populates='customer', lazy='dynamic')
    orders = db.relationship('Order', back_populates='customer', cascade='all, delete', lazy=True)
    parts = db.relationship('Part', back_populates='customer')

class Part(db.Model):
    __tablename__ = 'parts'
    __table_args__ = {'extend_existing': True}

    part_number = db.Column(db.String(255), primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.customer_id'), nullable=False)
    part_description = db.Column(db.String(255), nullable=False)
    anodising_duration = db.Column(db.Integer, nullable=True)
    anodising_selection_status = db.Column(db.Integer, nullable=False, default=0)
    voltage = db.Column(db.Float, nullable=True)
    voltage_selection_status = db.Column(db.Integer, nullable=False, default=0)
    etch = db.Column(db.Float, nullable=True)
    etch_selection_status = db.Column(db.Integer, nullable=False, default=0)
    strip_etch = db.Column(db.Float, nullable=True)
    strip_etch_selection_status = db.Column(db.Integer, nullable=False, default=0)
    sealing = db.Column(db.String(50), nullable=True)
    sealing_selection_status = db.Column(db.Integer, nullable=False, default=0)
    dye = db.Column(db.String(50), nullable=True)
    dye_selection_status = db.Column(db.Integer, nullable=False, default=0)
    double_and_etch = db.Column(db.String(3), nullable=True)
    double_and_etch_selection_status = db.Column(db.Integer, nullable=False, default=0)
    polishing = db.Column(JSON, nullable=True)  # Polishing steps as JSON
    polishing_selection_status = db.Column(db.Integer, nullable=False, default=0)
    blasting = db.Column(db.String(50), nullable=True)
    blasting_selection_status = db.Column(db.Integer, nullable=False, default=0)
    brightening = db.Column(db.Float, nullable=True)
    brightening_selection_status = db.Column(db.Integer, nullable=False, default=0)
    jig_type = db.Column(db.String(100), db.ForeignKey('jigs_inventory.jig_type'), nullable=True)
    custom_upj = db.Column(db.Integer, nullable=True)
    custom_jpl = db.Column(db.Integer, nullable=True)
    custom_mpj = db.Column(db.Integer, nullable=True)
    image = db.Column(db.String(1024), nullable=True)

    order_lines = db.relationship('OrderLine', backref='part', lazy=True)
    customer = db.relationship('Customer', back_populates='parts')
    jig = db.relationship('Jig', backref='parts')

    __table_args__ = (
        CheckConstraint('anodising_duration IS NULL OR (anodising_duration >= 5 AND anodising_duration <= 90)', name='check_anodising_duration'),
        CheckConstraint('(voltage IS NULL OR (voltage >= 12.5 AND voltage <= 20))', name='check_voltage_range'),
        CheckConstraint('double_and_etch IS NULL OR double_and_etch IN ("Yes", "No")', name='check_double_and_etch'),
        CheckConstraint('brightening IS NULL OR brightening >= 0', name='check_brightening_duration'),
        CheckConstraint("anodising_selection_status IN (1, 0)", name='check_anodising_selection_status'),
        CheckConstraint("voltage_selection_status IN (1, 0)", name='check_voltage_selection_status'),
        CheckConstraint("etch_selection_status IN (1, 0)", name='check_etch_selection_status'),
        CheckConstraint("strip_etch_selection_status IN (1, 0)", name='check_etch_selection_status'),
        CheckConstraint("sealing_selection_status IN (1, 0)", name='check_sealing_selection_status'),
        CheckConstraint("dye_selection_status IN (1, 0)", name='check_dye_selection_status'),
        CheckConstraint("polishing_selection_status IN (1, 0)", name='check_polishing_selection_status'),
        CheckConstraint("blasting_selection_status IN (1, 0)", name='check_blasting_selection_status'),
        CheckConstraint("brightening_selection_status IN (1, 0)", name='check_brightening_selection_status'),
        CheckConstraint("double_and_etch_selection_status IN (1, 0)", name='check_double_and_etch_selection_status')
    )

class Order(db.Model):
    __tablename__ = 'orders'
    __table_args__ = {'extend_existing': True}

    order_id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.customer_id'), nullable=False)
    purchase_order_number = db.Column(db.String(255), nullable=False)
    date_of_arrival = db.Column(db.Date, nullable=False)
    collection_method = db.Column(db.String(50), nullable=False)
    status = db.Column(db.String(50), nullable=False)

    # Relationships
    customer = db.relationship('Customer', back_populates='orders')
    order_lines = db.relationship('OrderLine', cascade='all, delete-orphan', lazy='dynamic')

    __table_args__ = (
        CheckConstraint("status IN ('In Progress', 'Complete')", name='check_order_status'),
    )

class OrderLine(db.Model):
    __tablename__ = 'OrderLine'
    __table_args__ = {'extend_existing': True}

    OrderLine_id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('orders.order_id'), nullable=False)
    part_number = db.Column(db.String(255), db.ForeignKey('parts.part_number'), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    unit_price = db.Column(db.Numeric(18, 2), nullable=False)
    lot_price = db.Column(db.Numeric(18, 2), nullable=False)
    vat = db.Column(db.Numeric(18, 2), nullable=False)
    total_price = db.Column(db.Numeric(18, 2), nullable=False)

# PROCESS_CATEGORIES Dictionary  
PROCESS_CATEGORIES = {
    "jigging": "operation",
    "loading": "operation",
    "degrease": "operation",
    "anodising": "operation",
    "water rinse 1": "operation",
    "water rinse 2": "operation",
    "water rinse 3": "operation",
    "water rinse 4": "operation",
    "water rinse 5": "operation",
    "water rinse 6": "operation",
    "desmut": "operation",
    "caustic etch": "operation",
    "brightening": "operation",
    "cold seal": "operation",
    "unloading": "operation",
    "drying": "operation",
    "unjigging": "operation",
    "packing": "operation",
    "polishing": "operation",
    "blasting": "operation",
    "inspection": "operation",
    "stripping": "operation",
    "flash anodising": "operation",
    "default (un-dyed)": "operation",
    "black rinse": "operation",
    "off-line rinse": "operation",
    "Gold": "operation",
    "Black": "operation",
    "Premium Black": "operation",
    "Blue": "operation",
    "Turquoise": "operation",
    "Green": "operation",
    "Bronze": "operation",
    "Red": "operation",
    "Orange": "operation",
    "rinse (gold)": "operation",
    "rinse (black)": "operation"
}

# DYE_CATEGORIES Dictionary 
DYE_CATEGORIES = {
    "Gold": "in-line",
    "Black": "in-line",
    "Premium Black": "off-line",
    "Blue": "off-line",
    "Turquoise": "off-line",
    "Stainless": "off-line",
    "Green": "off-line",
    "Bronze": "off-line",
    "Red": "off-line",
    "Orange": "off-line",
    "Default (un-dyed)": "none",
    "No": "none"
}

def categorize_dye(dye_name):
    return DYE_CATEGORIES.get(dye_name, "off-line")

class GanttJob(db.Model):
    __tablename__ = 'gantt_jobs'

    gantt_job_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    component_job_id = db.Column(db.Integer, db.ForeignKey('component_jobs.component_job_id'), nullable=False, unique=True)
    order_id = db.Column(db.Integer, db.ForeignKey('orders.order_id'), nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.customer_id'), nullable=False)
    load_number = db.Column(db.Integer, nullable=False)

    # Process Step Start & End Timestamps
    polishing_start = db.Column(db.DateTime, nullable=True)
    polishing_end = db.Column(db.DateTime, nullable=True)

    blasting_start = db.Column(db.DateTime, nullable=True)
    blasting_end = db.Column(db.DateTime, nullable=True)

    brightening_start = db.Column(db.DateTime, nullable=True)
    brightening_end = db.Column(db.DateTime, nullable=True)

    off_line_rinse_start = db.Column(db.DateTime, nullable=True)
    off_line_rinse_end = db.Column(db.DateTime, nullable=True)

    jigging_start = db.Column(db.DateTime, nullable=True)
    jigging_end = db.Column(db.DateTime, nullable=True)

    loading_start = db.Column(db.DateTime, nullable=True)
    loading_end = db.Column(db.DateTime, nullable=True)

    degrease_start = db.Column(db.DateTime, nullable=True)
    degrease_end = db.Column(db.DateTime, nullable=True)

    water_rinse_1_start = db.Column(db.DateTime, nullable=True)
    water_rinse_1_end = db.Column(db.DateTime, nullable=True)

    water_rinse_2_start = db.Column(db.DateTime, nullable=True)
    water_rinse_2_end = db.Column(db.DateTime, nullable=True)

    etch_start = db.Column(db.DateTime, nullable=True)
    etch_end = db.Column(db.DateTime, nullable=True)

    water_rinse_3_start = db.Column(db.DateTime, nullable=True)
    water_rinse_3_end = db.Column(db.DateTime, nullable=True)

    water_rinse_4_start = db.Column(db.DateTime, nullable=True)
    water_rinse_4_end = db.Column(db.DateTime, nullable=True)

    desmut_start = db.Column(db.DateTime, nullable=True)
    desmut_end = db.Column(db.DateTime, nullable=True)

    water_rinse_5_start = db.Column(db.DateTime, nullable=True)
    water_rinse_5_end = db.Column(db.DateTime, nullable=True)

    water_rinse_6_start = db.Column(db.DateTime, nullable=True)
    water_rinse_6_end = db.Column(db.DateTime, nullable=True)

    anodising_1a_start = db.Column(db.DateTime, nullable=True)
    anodising_1a_end = db.Column(db.DateTime, nullable=True)

    anodising_1b_start = db.Column(db.DateTime, nullable=True)
    anodising_1b_end = db.Column(db.DateTime, nullable=True)

    anodising_2a_start = db.Column(db.DateTime, nullable=True)
    anodising_2a_end = db.Column(db.DateTime, nullable=True)

    anodising_2b_start = db.Column(db.DateTime, nullable=True)
    anodising_2b_end = db.Column(db.DateTime, nullable=True)

    water_rinse_7_start = db.Column(db.DateTime, nullable=True)
    water_rinse_7_end = db.Column(db.DateTime, nullable=True)

    water_rinse_8_start = db.Column(db.DateTime, nullable=True)
    water_rinse_8_end = db.Column(db.DateTime, nullable=True)

    gold_dye_start = db.Column(db.DateTime, nullable=True)
    gold_dye_end = db.Column(db.DateTime, nullable=True)

    black_dye_start = db.Column(db.DateTime, nullable=True)
    black_dye_end = db.Column(db.DateTime, nullable=True)

    sealing_start = db.Column(db.DateTime, nullable=True)
    sealing_end = db.Column(db.DateTime, nullable=True)

    cold_seal_a_start = db.Column(db.DateTime, nullable=True)
    cold_seal_a_end = db.Column(db.DateTime, nullable=True)

    cold_seal_b_start = db.Column(db.DateTime, nullable=True)
    cold_seal_b_end = db.Column(db.DateTime, nullable=True)

    boiling_water_seal_start = db.Column(db.DateTime, nullable=True)
    boiling_water_seal_end = db.Column(db.DateTime, nullable=True)

    unloading_start = db.Column(db.DateTime, nullable=True)
    unloading_end = db.Column(db.DateTime, nullable=True)

    dye_offline_start = db.Column(db.DateTime, nullable=True)
    dye_offline_end = db.Column(db.DateTime, nullable=True)

    hot_seal_start = db.Column(db.DateTime, nullable=True)
    hot_seal_end = db.Column(db.DateTime, nullable=True)

    drying_start = db.Column(db.DateTime, nullable=True)
    drying_end = db.Column(db.DateTime, nullable=True)

    unjigging_start = db.Column(db.DateTime, nullable=True)
    unjigging_end = db.Column(db.DateTime, nullable=True)

    packing_start = db.Column(db.DateTime, nullable=True)
    packing_end = db.Column(db.DateTime, nullable=True)

    # Relationships
    component_job = db.relationship('ComponentJob', backref=db.backref('gantt_jobs', lazy=True))
    order = db.relationship('Order', backref='gantt_jobs', lazy=True)
    customer = db.relationship('Customer', backref='gantt_jobs', lazy=True)

    # Auto-delete records older than 2 days
    @staticmethod
    def delete_old_records():
        """Delete GanttJobs older than 2 days."""
        threshold_date = datetime.utcnow() - timedelta(days=2)
        db.session.query(GanttJob).filter(GanttJob.jigging_start < threshold_date).delete()
        db.session.commit()
        logger.info("Deleted old Gantt Jobs older than 2 days.")


class ComponentJob(db.Model):
    __tablename__ = 'component_jobs'

    component_job_id = db.Column(db.Integer, primary_key=True)
    part_id = db.Column(db.String(255), db.ForeignKey('parts.part_number'), nullable=False)
    order_line_id = db.Column(db.Integer, db.ForeignKey('OrderLine.OrderLine_id'), nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.customer_id'), nullable=False)

    required_jigs = db.Column(db.Integer, nullable=False)
    loads_required = db.Column(db.Integer, nullable=False)
    buzzbars_required = db.Column(db.Float, nullable=False)
    load_independent_operations = db.Column(JSON, nullable=True) 
    operations = db.Column(JSON, nullable=True) 
    jigging_duration_per_load = db.Column(db.Integer, nullable=False, default=0)  


    customer_name = db.Column(db.String(255), nullable=False)
    units_per_load = db.Column(db.Integer, nullable=False)
    quantity_of_final_load = db.Column(db.Integer, nullable=False)

    # Relationships
    customer = db.relationship('Customer', back_populates='component_jobs')
    part = db.relationship('Part', backref='component_jobs')
    order_line = db.relationship('OrderLine', backref='component_jobs')

    @staticmethod
    def add_operation(operation_name, duration, loads_required, additional_info=None):
        """Adds an operation with the given details."""
        description = operation_name.lower()
        if additional_info:
            description += f" ({additional_info})"

        operation_data = {
            "operation": operation_name,
            "duration": duration,
            "description": description,
            "initials": [f"Load {i + 1}" for i in range(loads_required)]
        }
        return operation_data


    @staticmethod
    def fetch_jig_for_part(part):
        """Helper function to fetch the associated jig based on part's jig_type."""
        if part.jig_type:
            # Strip whitespace from jig_type and log the value
            jig_type = part.jig_type.strip()
            logger.info(f"Looking up jig for jig_type: '{jig_type}'")

            # Perform the lookup
            jig = Jig.query.filter_by(jig_type=jig_type).first()

            # Check if jig is found
            if jig:
                logger.info(f"Found jig for jig_type: '{jig_type}' with ID: {jig.jig_id}")
                return jig
            else:
                logger.warning(f"No jig found for jig_type: '{jig_type}'. Check if jig_type exists in database or if there are whitespace issues.")
        else:
            logger.warning("Part has no jig_type specified.")

        # Return None if no jig is found
        return None

    @staticmethod
    def determine_jig_values(part, jig):
        """Helper function to determine UPJ, JPL, and MPJ with fallback logic."""
        upj = part.custom_upj or (jig.maxUPJ if jig else 5)  # Default fallback
        jpl = part.custom_jpl or (jig.maxJPL if jig else 10) # Default fallback
        mpj = part.custom_mpj or (jig.MPJ if jig else 2)     # Default  fallback

        if jig:
            logger.info(f"Using jig values for part '{part.part_number}' from jig type '{jig.jig_type}' - UPJ: {upj}, JPL: {jpl}, MPJ: {mpj}")
        else:
            logger.warning(f"No jig found, using default values for part '{part.part_number}' - UPJ: {upj}, JPL: {jpl}, MPJ: {mpj}")

        return upj, jpl, mpj

    @staticmethod
    def generate_component_jobs(order):
        """Generates component jobs from an Order object, and its related OrderLine and Part objects."""
        component_jobs = []
        index = 1  # Line index within the order

        for order_line in order.order_lines:
            part = order_line.part

            if not part:
                logger.error(f"No part found for order line {order_line.OrderLine_id}. Skipping this line.")
                continue

            # Fetch jig and its data
            jig = ComponentJob.fetch_jig_for_part(part)
            if not jig:
                logger.warning(f"Jig type '{part.jig_type}' not found in database.")
                continue

            # Determine UPJ, JPL, and MPJ values with fallback
            upj, jpl, mpj = ComponentJob.determine_jig_values(part, jig)

            # Calculate required jigs, buzzbars, and loads
            quantity = order_line.quantity
            required_jigs = math.ceil(quantity / upj)
            buzzbars_required = required_jigs / jpl
            loads_required = math.ceil(required_jigs / jpl)
            # Calculate units per load (UPJ * JPL)
            units_per_load = upj * jpl

            # Determine the quantity of the final load
            if loads_required > 1:
                # Multi-load job: Calculate the remaining quantity for the final load
                quantity_of_final_load = quantity - (units_per_load * (loads_required - 1))
            else:
                # Single load job: The final load is the total quantity
                quantity_of_final_load = quantity

            # Calculate durations and operations
            jigging_duration_per_load = math.ceil(mpj * required_jigs / loads_required)
            total_jigging_duration = jigging_duration_per_load * loads_required
            packing_duration = math.ceil(mpj / 3 * required_jigs / loads_required)

            # List to hold load-independent operations
            load_independent_operations = []

            # Parse polishing details
            try:
                polishing_details = json.loads(part.polishing) if part.polishing else []
            except json.JSONDecodeError:
                polishing_details = []  # Default to an empty list if parsing fails

            # Polishing Operations
            if part.polishing_selection_status == 1:
                for step in polishing_details:
                    load_independent_operations.append({
                        "operation": f"Polishing; Step {step['step_number']}, Equipment: {step['equipment']}, Grit: {step['grit']}, Compound: {step['compound']}",
                        "duration": 4 * total_jigging_duration,  # Ensure this reflects polishing-specific logic
                        "notes": f"DD/MM & Initial(s) & Quantity: {quantity}"  # Placeholder for operator notes
                    })

            # Blasting Operations
            if part.blasting_selection_status == 1:
                calculated_blasting_duration = 2 * total_jigging_duration  # Replace with actual logic if necessary
                load_independent_operations.append({
                    "operation": f"Blasting ({part.blasting})",
                    "duration": calculated_blasting_duration,
                    "notes": f"DD/MM & Initial(s) & Quantity: {quantity}"  # Placeholder for operator notes
                })

            # List to hold operations
            operations = []

            operations.append(ComponentJob.add_operation("Jigging", jigging_duration_per_load, loads_required))

            # Check for Strip Etch operation
            if part.strip_etch_selection_status == 1:
                operations.append(ComponentJob.add_operation("Strip Etch", part.strip_etch, loads_required))

            # Determine next steps based on anodising selection status
            if part.anodising_selection_status == 1:
                # Continue with the anodic process
                operations.append(ComponentJob.add_operation("Loading", 1, loads_required))
                operations.append(ComponentJob.add_operation("Degrease", 10, loads_required))
                operations.append(ComponentJob.add_operation("Water Rinse (1 or 2)", 1, loads_required))

                # Anodising and etching operations
                if part.double_and_etch_selection_status == 1:
                    operations.append(ComponentJob.add_operation("Caustic Etch", 0.25, loads_required))
                    operations.append(ComponentJob.add_operation("Water Rinse (1 or 2)", 1, loads_required))
                    operations.append(ComponentJob.add_operation("Flash Anodise", 5, loads_required, additional_info="16V"))
                    operations.append(ComponentJob.add_operation("Water Rinse (3 or 4)", 1, loads_required))
                    operations.append(ComponentJob.add_operation("Caustic Etch", 3, loads_required))

                if part.etch_selection_status == 1:
                    operations.append(ComponentJob.add_operation("Caustic Etch", part.etch, loads_required))
                    operations.append(ComponentJob.add_operation("Water Rinse (2 or 1)", 1, loads_required))
                    operations.append(ComponentJob.add_operation("Desmut", 1, loads_required))
                    operations.append(ComponentJob.add_operation("Water Rinse (3 or 4)", 1, loads_required))

                # Anodising process
                anodise_duration = part.anodising_duration or 0
                voltage = part.voltage or "N/A"
                operations.append(ComponentJob.add_operation("Anodising", anodise_duration, loads_required, additional_info=f"{voltage}V"))
                operations.append(ComponentJob.add_operation("Water Rinse (5 or 6)", 1, loads_required))

            else:
                # Skip anodising and proceed with drying, unjigging, and packing
                operations.append(ComponentJob.add_operation("Drying", 15, loads_required))
                operations.append(ComponentJob.add_operation("Unjigging", math.ceil(2.5 * required_jigs / loads_required), loads_required))
                operations.append(ComponentJob.add_operation("Packing", packing_duration, loads_required))

            # Dye category (undyed or in-line/off-line  dyed)
            dye = part.dye.capitalize() if part.dye else "Default (un-dyed)"
            dye_category = categorize_dye(dye)

            # Initialize unloading tracking
            unloading_done = False

            # Handle off-line dyeing (includes hot seal)
            if part.dye_selection_status == 1 and dye_category == "off-line":      
                operations.append(ComponentJob.add_operation("Unloading", 1, loads_required))
                unloading_done = True

                # Off-line dye, rinse, and hot seal
                operations.append(ComponentJob.add_operation(f"{dye}", 20, loads_required))
                operations.append(ComponentJob.add_operation("Off-line rinse", 1, loads_required))
                operations.append(ComponentJob.add_operation("Hot Seal", 30, loads_required))
                operations.append(ComponentJob.add_operation("Off-line rinse", 1, loads_required))

            # Handle in-line dyeing
            elif part.dye_selection_status == 1 and dye_category == "in-line":
                # In-line dye, rinse, seal, and rinse
                operations.append(ComponentJob.add_operation(f"{dye}", 20, loads_required))
                operations.append(ComponentJob.add_operation("Water Rinse (7)", 1, loads_required))
                operations.append(ComponentJob.add_operation(part.sealing, 30 if "30 min" in part.sealing or "Boiling" in part.sealing else 15, loads_required))
                operations.append(ComponentJob.add_operation("Water Rinse (8)", 1, loads_required))

            # Handle undyed parts (dye_selection_status = 0)
            elif part.dye_selection_status == 0:
                # Check if the part requires unloading and then hot sealing
                if part.sealing == "Hot Seal":
                    operations.append(ComponentJob.add_operation("Unloading", "1", loads_required))
                    unloading_done = True
                    operations.append(ComponentJob.add_operation("Hot Seal", 30, loads_required))
                    operations.append(ComponentJob.add_operation("Off-line rinse", 1, loads_required))
                else:
                    # Seal and rinse without unloading
                    operations.append(ComponentJob.add_operation(part.sealing, 30 if "30 min" in part.sealing or "Boiling" in part.sealing else 15, loads_required))
                    operations.append(ComponentJob.add_operation("Water Rinse (8)", 1, loads_required))

            # Final unloading (only if it hasn’t already happened)
            if not unloading_done:
                operations.append(ComponentJob.add_operation("Unloading", 1, loads_required))

            # Final post-processing steps
            operations.append(ComponentJob.add_operation("Drying", 15, loads_required))
            operations.append(ComponentJob.add_operation("Unjigging", math.ceil(2.5 * required_jigs / loads_required), loads_required))
            operations.append(ComponentJob.add_operation("Packing", packing_duration, loads_required))



            # Create and store component job
            component_job = ComponentJob(
                part_id=part.part_number,
                order_line_id=order_line.OrderLine_id,
                customer_name=order.customer.customer_name,
                customer_id=order.customer.customer_id,
                required_jigs=required_jigs,
                buzzbars_required=buzzbars_required,
                loads_required=loads_required,
                units_per_load=units_per_load,  
                quantity_of_final_load=quantity_of_final_load, 
                operations=operations,
                load_independent_operations=load_independent_operations, 
                jigging_duration_per_load=jigging_duration_per_load
            )

            db.session.add(component_job)
            component_jobs.append(component_job)
            index += 1

        db.session.commit()  # Commit all  component jobs to the database
        return component_jobs


