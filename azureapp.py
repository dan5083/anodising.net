from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, send_from_directory, make_response
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate # type: ignore
from sqlalchemy.exc import SQLAlchemyError
import pandas as pd
from math import ceil
import io
import base64
from datetime import datetime, timedelta
import logging
import queue
import atexit
from markupsafe import Markup
from azure.storage.blob import BlobServiceClient # type: ignore
from jinja2 import TemplateNotFound
import json
import os
from itertools import chain
from tenacity import retry, stop_after_attempt, wait_exponential # type: ignore
from contextlib import contextmanager
import traceback
import requests # type: ignore
from urllib.parse import quote_plus
from azure.core.exceptions import AzureError # type: ignore
from sqlalchemy import create_engine, text, event
from sqlalchemy.orm import joinedload
from sqlalchemy import or_
from sqlalchemy.engine import URL
from sqlalchemy.inspection import inspect
import pyodbc # type: ignore
from werkzeug.utils import secure_filename
from werkzeug.security import check_password_hash
from models import (db, logger, Order, Customer,  # type: ignore
                        OrderLine, Part,ComponentJob, Jig, User, GanttJob)

# Define the database URI construction function
def get_sqlalchemy_database_uri():
    """
    Constructs the SQLAlchemy database URI for Azure SQL Database using SQL Authentication.
    """
    SQL_SERVER = os.getenv('AZURE_SQL_SERVER')
    SQL_DATABASE = os.getenv('AZURE_SQL_DATABASE')
    SQL_PORT = os.getenv('AZURE_SQL_PORT')
    SQL_USERNAME = os.getenv('SQL_ADMINISTRATOR_LOGIN')
    SQL_PASSWORD = os.getenv('SQL_AUTHENTICATION_PASSWORD')

    connection_string = (
        f"Driver={{ODBC Driver 18 for SQL Server}};"
        f"Server=tcp:{SQL_SERVER},{SQL_PORT};"
        f"Database={SQL_DATABASE};"
        f"Uid={SQL_USERNAME};"
        f"Pwd={SQL_PASSWORD};"
        f"Encrypt=yes;"
        f"TrustServerCertificate=no;"
        f"Connection Timeout=30;"
    )

    return f"mssql+pyodbc:///?odbc_connect={quote_plus(connection_string)}"

# Initialize Flask app
app = Flask(__name__, static_folder='Static/Data', template_folder='templates')

# Flask configuration
app.secret_key = os.getenv('SECRET_KEY', 'default_secret_key')
app.config['SESSION_TYPE'] = 'filesystem'
app.config['SESSION_PERMANENT'] = False
app.config['SESSION_USE_SIGNER'] = True
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Set SQLAlchemy Database URI
app.config['SQLALCHEMY_DATABASE_URI'] = get_sqlalchemy_database_uri()

# Optional: Log the database URI (mask sensitive parts in production)
logger.info(f"Database URI set: {app.config['SQLALCHEMY_DATABASE_URI']}")

# Initialize Flask-Migrate
migrate = Migrate()

# Link SQLAlchemy and Flask-Migrate to the Flask app
db.init_app(app)
migrate.init_app(app, db)


logger.info("SQLAlchemy and Flask-Migrate initialized successfully.")

# BlobServiceClient initialization
SAS_URL = os.getenv('BLOB_SERVICE_SAS_URL')  # SAS URL for secure access
ACCOUNT_URL = os.getenv('AZURE_BLOB_ACCOUNT_URL')  # Account URL as fallback
CONTAINER_NAME = os.getenv('AZURE_BLOB_CONTAINER_NAME')  # Container name from env
BLOB_SERVICE_CLIENT = None

try:
    if not CONTAINER_NAME:
        raise ValueError("The 'AZURE_BLOB_CONTAINER_NAME' environment variable is not set.")

    if SAS_URL:
        # Use SAS Token URL if provided
        logger.info("Initializing BlobServiceClient with SAS Token URL")
        BLOB_SERVICE_CLIENT = BlobServiceClient(account_url=SAS_URL)
    elif ACCOUNT_URL:
        # Fallback to Account URL
        logger.info("Initializing BlobServiceClient with Account URL")
        BLOB_SERVICE_CLIENT = BlobServiceClient(account_url=ACCOUNT_URL)
    else:
        # Raise error if no valid configuration is found
        raise ValueError("No valid Azure Blob Storage configuration found. Ensure 'BLOB_SERVICE_SAS_URL' or 'AZURE_BLOB_ACCOUNT_URL' is set.")

    # Test connection to the container
    container_client = BLOB_SERVICE_CLIENT.get_container_client(CONTAINER_NAME)
    blobs = list(container_client.list_blobs())
    logger.info(f"Connected to container: {CONTAINER_NAME}. Found {len(blobs)} blobs.")
except AzureError as e:
    logger.error(f"Failed to initialize BlobServiceClient: {e}")
    BLOB_SERVICE_CLIENT = None
except ValueError as e:
    logger.error(str(e))
    BLOB_SERVICE_CLIENT = None

COLUMN_MAPPING = {
    "Packing": ["packing_start", "packing_end"],
    "Unjigging": ["unjigging_start", "unjigging_end"],
    "Drying": ["drying_start", "drying_end"],
    
    # ✅ Newly Added Steps: "Off-line Dye" and "Hot Seal" after unloading
    "Hot Seal": ["hot_seal_start", "hot_seal_end"],
    "Off-line Dye": ["dye_offline_start", "dye_offline_end"],

    # ✅ Final Steps
    "Unloading": ["unloading_start", "unloading_end"],

    # ✅ Sealing and Dyeing Process
    "Black Dye": ["black_dye_start", "black_dye_end"],
    "Gold Dye": ["gold_dye_start", "gold_dye_end"],
    "Boiling Water Seal": ["boiling_water_seal_start", "boiling_water_seal_end"],

    # ✅ Water Rinse (8) moved after Cold Sealing
    "Water Rinse (8)": ["water_rinse_8_start", "water_rinse_8_end"],

    # ✅ Cold Seal Steps (Now Properly Mapped)
    "Cold Seal B": ["cold_seal_b_start", "cold_seal_b_end"],
    "Cold Seal A": ["cold_seal_a_start", "cold_seal_a_end"],

    # ✅ Water Rinse Steps moved after Anodising
    "Water Rinse 6": ["water_rinse_6_start", "water_rinse_6_end"],
    "Water Rinse 5": ["water_rinse_5_start", "water_rinse_5_end"],

    # ✅ 🏭 Anodising Tanks (Explicitly Defined)
    "Anodising 2B": ["anodising_2b_start", "anodising_2b_end"],
    "Anodising 2A": ["anodising_2a_start", "anodising_2a_end"],
    "Anodising 1B": ["anodising_1b_start", "anodising_1b_end"],
    "Anodising 1A": ["anodising_1a_start", "anodising_1a_end"],

    # ✅ Chemical Treatments
    "Desmut": ["desmut_start", "desmut_end"],
    "Caustic Etch": ["etch_start", "etch_end"],

    # ✅ Individual Water Rinse Steps (No more nested lists)
    "Water Rinse 4": ["water_rinse_4_start", "water_rinse_4_end"],
    "Water Rinse 3": ["water_rinse_3_start", "water_rinse_3_end"],
    "Water Rinse 2": ["water_rinse_2_start", "water_rinse_2_end"],
    "Water Rinse 1": ["water_rinse_1_start", "water_rinse_1_end"],

    # ✅ Standard Process Steps
    "Degrease": ["degrease_start", "degrease_end"],
    "Loading": ["loading_start", "loading_end"],
    "Jigging": ["jigging_start", "jigging_end"],
    "Brightening": ["brightening_start", "brightening_end"],
    "Blasting": ["blasting_start", "blasting_end"],
    "Polishing": ["polishing_start", "polishing_end"]
}

# Run the app if executed directly
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)

@app.route('/favicon.ico')
def favicon():
    return send_from_directory(
        'Static/Data',  
        'favicon.ico',
        mimetype='image/vnd.microsoft.icon'
    )

def upload_image_to_blob(image_file, part_number, blob_service_client, blob_container):
    """
    Uploads an image to Azure Blob Storage and returns the full URL of the uploaded image
    with a hardcoded URL prefix.
    """
    try:
        # Generate a clean filename based on the part number
        image_name = secure_filename(f"{part_number}.png")

        # Create a blob client for the target container and blob name
        blob_client = blob_service_client.get_blob_client(
            container=blob_container,
            blob=f'jigged_parts_images/{image_name}'
        )

        # Upload the file to Blob Storage
        blob_client.upload_blob(image_file, overwrite=True)
        logger.info(f"Image '{image_name}' uploaded successfully to Blob Storage.")

        # Hardcoded URL prefix
        url_prefix = "https://danstoreacc.blob.core.windows.net/bamscontainer/jigged_parts_images/"
        image_url = f"{url_prefix}{image_name}"

        logger.info(f"Image URL: {image_url}")
        return image_url  # Return the full URL

    except AzureError as e:
        logger.error(f"Failed to upload image to Blob Storage: {e}")
        raise e
    except Exception as e:
        logger.error(f"An unexpected error occurred while uploading image: {e}")
        raise e


@app.route('/get_parts/<customer_id>')
def get_parts(customer_id):
    try:
        # Fetch parts associated with the selected customer
        parts = Part.query.filter_by(customer_id=customer_id).all()
        parts_data = [{"part_number": part.part_number, "part_description": part.part_description} for part in parts]
        return jsonify(parts_data), 200
    except Exception as e:
        logger.error(f"Error fetching parts for customer {customer_id}: {str(e)}")
        return jsonify({"error": "Failed to load parts"}), 500
    
@app.route('/get_part_details/<part_number>', methods=['GET', 'POST'])
def get_part_details(part_number):
    try:
        # Fetch the part from the database
        part = Part.query.filter_by(part_number=part_number).first()
        if not part:
            return jsonify({"error": "Part not found"}), 404

        # Parse the polishing field as JSON
        try:
            polishing_data = json.loads(part.polishing) if part.polishing else []
        except json.JSONDecodeError:
            polishing_data = []  # Fallback to an empty list if decoding fails

        # Prepare part details for response
        part_details = {
            "part_number": part.part_number,
            "part_description": part.part_description,
            "anodising_duration": part.anodising_duration,
            "voltage": part.voltage,
            "etch": part.etch,
            "sealing": part.sealing,
            "dye": part.dye,
            "double_and_etch": part.double_and_etch,
            "polishing": polishing_data,
            "blasting": part.blasting,
            "brightening": part.brightening,
            "jig_type": part.jig_type,
            "custom_upj": part.custom_upj,
            "custom_jpl": part.custom_jpl,
            "custom_mpj": part.custom_mpj,
            "image_url": part.image,
            "strip_etch": part.strip_etch,
        }

        # Log part details for debugging
        logger.info(f"Fetched part details: {part_details}")

        return jsonify(part_details), 200
    except Exception as e:
        logger.error(f"Error fetching part details for {part_number}: {str(e)}")
        return jsonify({"error": "Failed to fetch part details"}), 500

# Utility Functions
def log_exception(error, context):
    logger.error(f"{context} - {error}")
    logger.debug(traceback.format_exc())

# Load customers from the database
def load_customers():
    logger.info("Loading customers from the SQL Database")
    try:
        customers = [dict(customer_id=str(c.customer_id), customer_name=c.customer_name) for c in Customer.query.all()]
        logger.info(f"Successfully loaded {len(customers)} customers from the database")
        return [customer.to_dict() for customer in customers]  # Return as list of dicts
    except SQLAlchemyError as db_err:
        logger.error(f"Database error occurred: {db_err}")
    except Exception as err:
        logger.error(f"Other error occurred: {err}")
    return []  # Return empty list in case of failure

# Method to convert the SQLAlchemy object to a dictionary for returning JSON-like response
def customer_to_dict(self):
    return {
        "id": self.customer_id,
        "customer_name": self.customer_name
    }

Customer.to_dict = customer_to_dict  # Attach this method to the Customer model

# Update a customer or add a new customer to the database
def upload_customers_to_db(customers):
    logger.info("Updating customers in the SQL Database")
    try:
        for customer_data in customers:
            customer = Customer.query.filter_by(customer_name=customer_data["customer_name"]).first()
            
            if customer:  # Update existing customer
                customer.customer_name = customer_data["customer_name"]
            else:  # Add new customer
                new_customer = Customer(customer_name=customer_data["customer_name"])
                db.session.add(new_customer)
        
        db.session.commit()  # Commit changes to the database
        logger.info("Successfully updated/added customers to the database")
    except SQLAlchemyError as db_err:
        db.session.rollback()  # Rollback in case of failure
        logger.error(f"Database error occurred: {db_err}")
        raise db_err
    except Exception as err:
        logger.error(f"Failed to update customers data: {err}")
        raise err


# Flask Routes
@app.errorhandler(Exception)
def handle_exception(e):
    """Handles all uncaught exceptions."""
    logger.exception(f"Unhandled exception: {e}")
    try:
        return render_template("error.html", error=str(e)), 500
    except TemplateNotFound:
        return "<h1>500 Internal Server Error</h1><p>Something went wrong.</p>", 500


@app.errorhandler(500)
def internal_error(error):
    """Handles HTTP 500 errors."""
    logger.error(f"Internal server error: {error}")
    try:
        return render_template("500.html"), 500
    except TemplateNotFound:
        return "<h1>500 Internal Server Error</h1><p>Unexpected server error occurred.</p>", 500


@app.route('/', methods=['GET', 'POST'])
def index():
    logger.info("Accessed index route")

    if request.method == 'POST':
        # Handle login form submission
        username = request.form['username']
        password = request.form['password']

        # Query the user from the database
        user = User.query.filter_by(username=username).first()

        if user and check_password_hash(user.password_hash, password):
            session['user_id'] = user.id
            session['username'] = user.username
            flash('Logged in successfully!', 'success')
            logger.info(f"User {username} logged in successfully.")
            return redirect(url_for('index'))
        else:
            flash('Invalid username or password', 'danger')
            logger.warning(f"Failed login attempt for username: {username}")

    return render_template('index.html', username=session.get('username'))

@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    flash('You have been logged out.', 'info')
    logger.info("User logged out.")
    return redirect(url_for('index'))
    
def log_field_details(field_name, field_value):
    logger.info(f"Field: {field_name}, Value: {field_value}, Type: {type(field_value).__name__}")

def process_new_part(index, form_data, customer_id):
    """
    Handles the creation of new parts, including validation, default settings, 
    and deselection logic.

    Args:
        index (int): The index of the part in the form data lists.
        form_data (ImmutableMultiDict): The form data from the request.
        customer_id (int): The ID of the customer associated with the part.

    Returns:
        Part: The newly created Part instance.

    Raises:
        ValueError: If any required fields for the new part are missing or invalid.
    """

    deselection_map = {
        'anodising_not_required': {
            "No anodic treatment required": {
                'anodising_duration': 0,
                'voltage': 0
            },
            "Anodising is required": {
                'anodising_duration': 1,
                'voltage': 1
            }
        },
        'etch': {
            "0.0": 0,
            "2.5": 1,
            "3.0": 1,
            "4.0": 1,
            "5.0": 1,
            "6.0": 1,
            "7.0": 1,
            "8.0": 1,
            "9.0": 1,
            "10.0": 1,
            "11.0": 1,
            "12.0": 1,
            "13.0": 1,
            "14.0": 1,
            "15.0": 1
        },
        'sealing': {
            "No sealing": 0,
            "Cold Seal 30 min": 1,
            "Cold Seal 15 min": 1,
            "Hot Seal": 1,
            "Boiling Water Seal": 1
        },
        'dye': {
            "Default": 0,
            "Black": 1,
            "Gold": 1,
            "Premium Black": 1,
            "Blue": 1,
            "Turquoise": 1,
            "Stainless": 1,
            "Green": 1,
            "Bronze": 1,
            "Red": 1,
            "Orange": 1
        },
        'brightening': {
            "No": 0,
            "0.5": 1,
            "1.0": 1,
            "1.5": 1,
            "2.0": 1,
            "2.5": 1,
            "3.0": 1,
            "3.5": 1,
            "4.0": 1,
            "4.5": 1,
            "5.0": 1,
            "5.5": 1,
            "6.0": 1,
            "6.5": 1,
            "7.0": 1,
            "7.5": 1,
            "8.0": 1,
            "8.5": 1,
            "9.0": 1,
            "9.5": 1,
            "10.0": 1
        },
        'double_and_etch': {
            "No": 0,
            "Yes": 1
        },
        'polishing': {
            "No": 0,
            "Yes": 1
        },
        'blasting': {
            "No": 0,
            "7 Grit": 1,
            "13 Grit": 1
        },
        'advanced_customization': {
            "No": {
                'custom_upj': 0,
                'custom_jpl': 0,
                'custom_mpj': 0
            },
            "Yes": {
                'custom_upj': 'user_defined',
                'custom_jpl': 'user_defined',
                'custom_mpj': 'user_defined'
            }
        }
    }
    try:
        # Retrieve fields specific to new parts
        part_number_new = form_data.getlist('part_number_new[]')[index].strip()
        part_description = form_data.getlist('part_description[]')[index].strip()
        jig_type = form_data.getlist('jig_type[]')[index].strip()
        quantity = int(form_data.getlist('quantity[]')[index])

        # Validate required fields for new parts
        if not part_number_new:
            raise ValueError(f"Missing part number for new part at index {index + 1}.")
        if not part_description:
            raise ValueError(f"Missing part description for new part at index {index + 1}.")
        if not jig_type:
            raise ValueError(f"Missing jig type for new part at index {index + 1}.")

        logger.info(f"Processing new part: {part_number_new}, {part_description}, Jig Type: {jig_type}, Quantity: {quantity}")

        # Handle anodising selection logic
        anodising_required = form_data.getlist('anodising_required[]')[index]
        if anodising_required == "Anodising is required":
            anodising_duration = int(form_data.getlist('anodising_duration[]')[index])
            voltage = float(form_data.getlist('voltage[]')[index])
            anodising_selection_status = 1
            voltage_selection_status = 1
            strip_etch = None
            strip_etch_selection_status = 0
        elif anodising_required == "No anodic treatment required":
            anodising_duration = None
            voltage = None
            anodising_selection_status = 0
            voltage_selection_status = 0
            strip_etch = None
            strip_etch_selection_status = 0
        elif anodising_required == "Strip Only (for AA10)":
            anodising_duration = None
            voltage = None
            anodising_selection_status = 0
            voltage_selection_status = 0
            strip_etch = 1.0
            strip_etch_selection_status = 1
        elif anodising_required == "Strip Only (for AA25)":
            anodising_duration = None
            voltage = None
            anodising_selection_status = 0
            voltage_selection_status = 0
            strip_etch = 2.5
            strip_etch_selection_status = 1

        logger.info(f"Anodising selection processed: {anodising_required}, Duration: {anodising_duration}, Voltage: {voltage}")

        # Handle optional fields
        etch_value = float(form_data.getlist('etch[]')[index])
        etch_selection_status = 1 if etch_value > 0 else 0

        sealing = form_data.getlist('sealing[]')[index]
        sealing_selection_status = 1 if sealing != "No sealing" else 0

        dye = form_data.getlist('dye[]')[index]
        dye_selection_status = 1 if dye != "Default" else 0

        double_and_etch = form_data.getlist('double_and_etch[]')[index]
        double_and_etch_selection_status = 1 if double_and_etch == "Yes" else 0

        brightening = float(form_data.getlist('brightening[]')[index]) if form_data.getlist('brightening[]')[index] != "No" else 0.0
        brightening_selection_status = 1 if brightening > 0 else 0

        blasting = form_data.getlist('blasting[]')[index]
        blasting_selection_status = 1 if blasting != "No" else 0

        logger.info(f"Optional fields processed: Etch={etch_value}, Sealing={sealing}, Dye={dye}, Brightening={brightening}, Blasting={blasting}")

        # Handle polishing steps (if applicable)
        polishing = form_data.getlist('polishing[]')[index]
        polishing_selection_status = 1 if polishing == "Yes" else 0
        polishing_data = []

        if polishing_selection_status:
            # Loop through up to 3 polishing steps
            for step_index in range(1, 4):  # Assuming steps are named polishing_equipment_1, etc.
                equipment = form_data.get(f'polishing_equipment_{step_index}', "").strip()
                grit = form_data.get(f'polishing_grit_{step_index}', "").strip()
                compound = form_data.get(f'polishing_compound_{step_index}', "").strip()

                # Add step data if any field is non-empty
                if equipment or grit or compound:
                    polishing_data.append({
                        "step_number": step_index,
                        "equipment": equipment,
                        "grit": grit,
                        "compound": compound,
                    })

        # Log polishing data for debugging
        logger.info(f"Processed polishing data for part: {polishing_data}")

        # Convert polishing data to JSON if polishing is enabled, else keep it as None
        polishing_json = json.dumps(polishing_data) if polishing_selection_status else None

        # Retrieve advanced custom jig details
        custom_upj = int(form_data.getlist('custom_upj[]')[index]) if form_data.getlist('custom_upj[]')[index] else None
        custom_jpl = int(form_data.getlist('custom_jpl[]')[index]) if form_data.getlist('custom_jpl[]')[index] else None
        custom_mpj = int(form_data.getlist('custom_mpj[]')[index]) if form_data.getlist('custom_mpj[]')[index] else None

        logger.info(f"Custom jig details processed: UPJ={custom_upj}, JPL={custom_jpl}, MPJ={custom_mpj}")

        # Create and save the new part
        new_part = Part(
            part_number=part_number_new,  # Use `part_number_new` for new parts
            part_description=part_description,
            customer_id=customer_id,
            jig_type=jig_type,
            anodising_duration=anodising_duration,
            voltage=voltage,
            anodising_selection_status=anodising_selection_status,
            voltage_selection_status=voltage_selection_status,
            strip_etch=strip_etch,
            strip_etch_selection_status=strip_etch_selection_status,
            etch=etch_value,
            etch_selection_status=etch_selection_status,
            sealing=sealing,
            sealing_selection_status=sealing_selection_status,
            dye=dye,
            dye_selection_status=dye_selection_status,
            double_and_etch=double_and_etch,
            double_and_etch_selection_status=double_and_etch_selection_status,
            polishing=polishing_json,
            polishing_selection_status=polishing_selection_status,
            brightening=brightening,
            brightening_selection_status=brightening_selection_status,
            blasting=blasting,
            blasting_selection_status=blasting_selection_status,
            custom_upj=custom_upj,
            custom_jpl=custom_jpl,
            custom_mpj=custom_mpj
        )

        db.session.add(new_part)
        db.session.commit()
        logger.info(f"New part created successfully: {new_part.part_number}")

        # Return the part as a dictionary with all relevant fields
        return {
            "part_number": new_part.part_number,
            "part_description": new_part.part_description,
            "jig_type": new_part.jig_type,
            "quantity": quantity,
            "anodising_duration": new_part.anodising_duration,
            "voltage": new_part.voltage,
            "etch": new_part.etch,
            "sealing": new_part.sealing,
            "dye": new_part.dye,
            "double_and_etch": new_part.double_and_etch,
            "polishing": new_part.polishing,
            "blasting": new_part.blasting,
            "brightening": new_part.brightening,
            "strip_etch": new_part.strip_etch,
            "custom_upj": new_part.custom_upj,
            "custom_jpl": new_part.custom_jpl,
            "custom_mpj": new_part.custom_mpj,
        }

    except Exception as e:
        logger.info(f"Index: {index}, part_number_new[]: {form_data.getlist('part_number_new[]')}")
        logger.error(f"Error creating new part at index {index}: {e}")
        db.session.rollback()
        raise ValueError(f"An error occurred while processing new part: {e}")


def process_existing_part(index, form_data):
    """
    Handles the retrieval and validation of an existing part from the form data.

    Args:
        index (int): The index of the part in the form data lists.
        form_data (ImmutableMultiDict): The form data from the request.

    Returns:
        dict: A dictionary containing the part details (to be used in order line creation).

    Raises:
        ValueError: If the part does not exist or required details are missing.
    """
    try:
        # Fetch part number for the existing part using `use_existing_part[]` instead of `part_number[]`
        part_number = form_data.getlist('use_existing_part[]')[index].strip()

        logger.info(f"Processing existing part at index {index}: {part_number}")

        # Retrieve the part from the database
        part = Part.query.filter_by(part_number=part_number).first()

        if not part:
            raise ValueError(f"Part with number '{part_number}' not found.")

        # Convert the part object into a dictionary
        part_details = {
            "part_number": part.part_number,
            "part_description": part.part_description,
            "jig_type": part.jig_type,
            "anodising_duration": part.anodising_duration,
            "anodising_selection_status": part.anodising_selection_status,
            "voltage": part.voltage,
            "voltage_selection_status": part.voltage_selection_status,
            "etch": part.etch,
            "etch_selection_status": part.etch_selection_status,
            "strip_etch": part.strip_etch,
            "strip_etch_selection_status": part.strip_etch_selection_status,
            "sealing": part.sealing,
            "sealing_selection_status": part.sealing_selection_status,
            "dye": part.dye,
            "dye_selection_status": part.dye_selection_status,
            "double_and_etch": part.double_and_etch,
            "double_and_etch_selection_status": part.double_and_etch_selection_status,
            "polishing": part.polishing,
            "polishing_selection_status": part.polishing_selection_status,
            "blasting": part.blasting,
            "blasting_selection_status": part.blasting_selection_status,
            "brightening": part.brightening,
            "brightening_selection_status": part.brightening_selection_status,
            "custom_upj": part.custom_upj,
            "custom_jpl": part.custom_jpl,
            "custom_mpj": part.custom_mpj,
            "image": part.image,
        }

        logger.info(f"Successfully retrieved existing part: {part.part_number}")
        return part_details

    except IndexError as e:
        logger.error(f"Index out of range for form data at index {index}: {e}")
        raise ValueError(f"Index out of range for form data at index {index}: {e}")

    except Exception as e:
        logger.error(f"Error processing existing part at index {index}: {e}")
        raise ValueError(str(e))

    
@app.route('/save_draft_line', methods=['POST'])
def save_draft_line():
    """
    Save a draft line and customer/order details into the session.
    """
    # Get the new line data from the request
    new_line = request.json.get('line')

    # Get customer and order details from the request (optional)
    customer_and_order_details = request.json.get('customer_and_order_details')

    # Ensure the session has a place to store order lines
    if 'order_lines' not in session:
        session['order_lines'] = []

    # Add the new line to the session
    session['order_lines'].append(new_line)

    # Save customer and order details to the session,  if provided
    if customer_and_order_details:
        session['customer_and_order_details'] = customer_and_order_details

    # Mark the session as modified
    session.modified = True

    return jsonify({
        'status': 'success',
        'message': 'Line saved!',
        'lines': session['order_lines'],
        'customer_and_order_details': session.get('customer_and_order_details', {})
    }), 200

@app.route('/orders', methods=['GET', 'POST'])
def orders():
    logger.info("Accessed /orders route")

    if request.method == 'POST':
        try:
            logger.info("Processing new order")

            # Fetch form data
            customer_id = request.form.get('customer_id')
            purchase_order_number = request.form.get('purchase_order_number')
            date_of_arrival = request.form.get('date_of_arrival')
            collection_method = request.form.get('collection_method')

          # Validate required fields
            missing_fields = []
            if not customer_id:
                missing_fields.append("Customer Name")
            if not purchase_order_number:
                missing_fields.append("Purchase Order Number")
            if not date_of_arrival:
                missing_fields.append("Date of Arrival")
            if not collection_method:
                missing_fields.append("Collection Method")

            if missing_fields:
                error_message = f"Missing required order details: {', '.join(missing_fields)}"
                logger.error(error_message)
                flash(error_message, "danger")
                return redirect(url_for('orders'))

            # Check for duplicate purchase order number
            if Order.query.filter_by(purchase_order_number=purchase_order_number).first():
                error_message = f"An order with this purchase order number already exists: {purchase_order_number}"
                logger.warning(error_message)
                flash(error_message, "danger")
                return redirect(url_for('orders'))

            # **Save customer and order details to the session**
            session['customer_and_order_details'] = {
                "customer_id": customer_id,
                "purchase_order_number": purchase_order_number,
                "date_of_arrival": date_of_arrival,
                "collection_method": collection_method,
            }
            session.modified = True  # Mark session as modified
            logger.info("Customer and order details saved to session.")

            # Fetch customer object
            customer = Customer.query.filter_by(customer_id=customer_id).first()
            if not customer:
                error_message = f"Customer with ID {customer_id} does not exist."
                logger.error(error_message)
                flash(error_message, "danger")
                return redirect(url_for('orders'))

            # Create the Order instance
            order = Order(
                customer_id=customer.customer_id,
                purchase_order_number=purchase_order_number,
                date_of_arrival=date_of_arrival,
                collection_method=collection_method,
                status='In Progress',
            )
            db.session.add(order)
            db.session.commit()

            logger.info(f"Order created successfully: {order.order_id}")

            # Determine the number of parts using part_description[]
            part_descriptions = request.form.getlist('part_description[]')
            number_of_lines = len(part_descriptions)
            logger.info(f"Processing {number_of_lines} parts.")

            if number_of_lines == 0:
                raise ValueError("No parts provided in the form.")

            # Validate consistency of part-related fields
            fields_to_validate = ['part_description[]', 'use_existing_part[]', 'quantity[]', 'unit_price[]', 'lot_price[]']
            for field in fields_to_validate:
                if len(request.form.getlist(field)) != number_of_lines:
                    raise ValueError(f"Inconsistent data in form field: {field}")

            # Process each part
            for i in range(number_of_lines):
                part_description = part_descriptions[i].strip()
                use_existing = request.form.getlist('use_existing_part[]')[i]

                logger.info(f"Processing part {i + 1}: {part_description}, use_existing={use_existing}")

                # Check if the part is existing or new
                if use_existing and use_existing != "No":  # Non-empty and not "No" means existing
                    part = process_existing_part(index=i, form_data=request.form)
                else:
                    # Validate required fields for new parts
                    if not part_description:
                        raise ValueError(f"Missing part description for part at index {i + 1}.")

                    part = process_new_part(index=i, form_data=request.form, customer_id=customer.customer_id)

                # Ensure part is a dictionary (validation)
                if not isinstance(part, dict):
                    raise ValueError(f"Expected part to be a dictionary, got {type(part).__name__}.")


             # Extract pricing details
                quantity = int(request.form.getlist('quantity[]')[i])
                unit_price = float(request.form.getlist('unit_price[]')[i] or 0)
                lot_price = float(request.form.getlist('lot_price[]')[i] or 0)
                part_number = part["part_number"]

                if not unit_price and not lot_price:
                    raise ValueError(f"Provide either unit price or lot price for part {part_description}.")
                if not lot_price:
                    lot_price = unit_price * quantity
                if not unit_price:
                    unit_price = lot_price / quantity

                # Calculate VAT and adjust total_price to include VAT
                net_price = lot_price  # Net price before VAT
                vat = round(net_price * 0.2, 2)  # 20% VAT
                total_price = net_price + vat  # Gross price including VAT

                logger.info(f"Pricing for part {part_number} at index {i}: Quantity={quantity}, Unit Price={unit_price}, Lot Price={lot_price}, Net Price={net_price}, Total Price (Gross)={total_price}, VAT={vat}")

                # Create OrderLine
                order_line = OrderLine(
                    order_id=order.order_id,
                    part_number=part_number,
                    quantity=quantity,
                    unit_price=unit_price,
                    lot_price=lot_price,
                    total_price=total_price,
                    vat=vat,
                )
                db.session.add(order_line)
                logger.info(f"OrderLine created for part {part_number} at index {i}.")

            # Commit changes after processing all parts
            db.session.commit()
            logger.info("Order and associated parts saved successfully.")

                    # Clear session data for drafts after successful save
            session.pop('order_lines', None)
            session.pop('customer_and_order_details', None)

            # Generate component jobs
            ComponentJob.generate_component_jobs(order)
            logger.info("Component jobs generated successfully.")

            # Determine the type of parts in the order for success message
            new_parts_in_order = any(use_existing == "No" for use_existing in request.form.getlist('use_existing_part[]'))
            existing_parts_in_order = any(use_existing == "Yes" for use_existing in request.form.getlist('use_existing_part[]'))

            # Flash appropriate success messages
            if new_parts_in_order and existing_parts_in_order:
                flash('🎉 Order with new and existing parts saved successfully! 🚀', 'success')
            elif new_parts_in_order:
                flash('🎉 Order with new parts saved successfully! 🛠️', 'success')
            elif existing_parts_in_order:
                flash('🎉 Order with existing parts saved successfully! 🔄', 'success')
            else:
                flash('🎉 Order saved successfully! 😃', 'success')

        except Exception as e:
            db.session.rollback()
            logger.exception(f"Error processing order: {e}")
            flash(f"An error occurred while processing the order: {e}", "danger")
            return redirect(url_for('orders'))

    # Handle GET requests
    try:
        customers = [dict(customer_id=str(c.customer_id), customer_name=c.customer_name) for c in Customer.query.all()]
        jigs = Jig.query.all()

        # **Inject Prepopulated Customer/Order Details into Template Context**
        customer_and_order_details = session.get('customer_and_order_details', {})
        
        # Fetch existing draft lines from the session
        existing_lines = session.get('order_lines', [])

        return render_template(
            'orders.html',
            customers=customers,
            jigs=jigs,
            orders=orders,
            existing_lines=existing_lines,
            customer_and_order_details=customer_and_order_details  # Pass prepopulated details to the template
        )
    except Exception as e:
        logger.exception("Error loading order form data")
        flash(f"An error occurred while loading order form data: {e}", "danger")
        return render_template("error.html")
    
@app.route('/update_orderline/<int:order_line_id>', methods=['POST'])
def update_orderline(order_line_id):
    """Updates the order line's quantity or unit price."""
    try:
        data = request.json
        field = data.get("field")
        new_value = data.get("newValue")

        # Validate field type
        if field not in ["quantity", "unit_price"]:
            return jsonify({"success": False, "error": "Invalid field"}), 400

        # Fetch order line
        order_line = OrderLine.query.get(order_line_id)
        if not order_line:
            return jsonify({"success": False, "error": "OrderLine not found"}), 404

        # Update the field
        setattr(order_line, field, new_value)
        db.session.commit()
        
        return jsonify({"success": True})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/component_jobs', methods=['GET'])
def component_jobs():
    """View component jobs with filtering and inline editing (uses update_orderline helper)."""
    try:
        # 🔎 Get filter parameters from request
        customer_id = request.args.get('customer_id')
        jig_type = request.args.get('jig_type')
        dye_type = request.args.get('dye_type')  # New filter
        dye_colour = request.args.get('dye_colour')  # New filter
        sort_by = request.args.get('sort_by')  # New sorting parameter

        # 🟢 Start Query
        query = (
            db.session.query(ComponentJob, OrderLine, Order, Customer, Part)
            .join(OrderLine, ComponentJob.order_line_id == OrderLine.OrderLine_id)
            .join(Order, Order.order_id == OrderLine.order_id)
            .join(Customer, Customer.customer_id == Order.customer_id)
            .join(Part, Part.part_number == OrderLine.part_number)
            .filter(ComponentJob.customer_name.isnot(None))
        )

        # 🟢 Apply filters
        if customer_id:
            query = query.filter(Order.customer_id == customer_id)
        if jig_type:
            query = query.filter(Part.jig_type == jig_type)
        if dye_type:
            query = query.filter(Part.dye_selection_status == (1 if dye_type == 'in-line' else 0))
        if dye_colour:
            query = query.filter(Part.dye == dye_colour)

        # 🟢 Apply sorting
        if sort_by:
            if sort_by == 'date_asc':
                query = query.order_by(Order.date_of_arrival.asc())
            elif sort_by == 'date_desc':
                query = query.order_by(Order.date_of_arrival.desc())
            elif sort_by == 'anodising_duration_asc':
                query = query.order_by(Part.anodising_duration.asc())
            elif sort_by == 'anodising_duration_desc':
                query = query.order_by(Part.anodising_duration.desc())
            elif sort_by == 'loads_required_asc':
                query = query.order_by(ComponentJob.loads_required.asc())
            elif sort_by == 'loads_required_desc':
                query = query.order_by(ComponentJob.loads_required.desc())

        # 🟢 Fetch results
        results = query.all()

        # 🟢 Group jobs by customer and purchase order number
        grouped_jobs = {}
        for job, order_line, order, customer, part in results:
            key = (customer.customer_name, order.purchase_order_number)
            if key not in grouped_jobs:
                grouped_jobs[key] = []

            grouped_jobs[key].append({
                'component_job_id': job.component_job_id,
                'part_number': order_line.part_number,
                'part_description': part.part_description,
                'quantity': order_line.quantity,
                'unit_price': str(order_line.unit_price),
                'jig_type': part.jig_type or "N/A",
                'dye_type': "In-line" if part.dye_selection_status == 1 else "Off-line",
                'dye_colour': part.dye or "N/A",
                'anodising_duration': part.anodising_duration or "N/A",
                'jigging_duration_per_load': job.jigging_duration_per_load,
                'loads_required': job.loads_required,
                'order_line_id': order_line.OrderLine_id
            })

        # 🟢 Fetch unique dropdown values for filters
        customers = Customer.query.with_entities(Customer.customer_id, Customer.customer_name).all()
        jigs = db.session.query(Part.jig_type).distinct().all()

        return render_template(
            'component_jobs.html',
            grouped_jobs=grouped_jobs,
            customers=customers,
            jigs=[j[0] for j in jigs],  # Extract jig types from tuples
        )

    except Exception as e:
        logger.error(f"Error fetching component jobs: {str(e)}")
        flash("An error occurred while loading component jobs. Please try again.", "danger")
        return render_template('error.html')

@app.route('/component_job_details/<int:component_job_id>', methods=['GET'])
def component_job_details(component_job_id):  # Accept component_job_id as a parameter
    try:
        # Query the specific component job by ID
        component_job = db.session.query(ComponentJob).filter_by(component_job_id=component_job_id).first()

        if component_job:
            part = component_job.part  # Get the associated part
            order_line = component_job.order_line  # Get the associated order line
            jig = part.jig  # Get the associated jig (always included)

            # Decode polishing JSON
            try:
                polishing_details = json.loads(part.polishing) if part.polishing else []
            except json.JSONDecodeError as e:
                logger.error(f"Error decoding polishing JSON for part {part.part_number}: {e}")
                polishing_details = []

            # Fetch the related order through order_line
            order = db.session.query(Order).filter_by(order_id=order_line.order_id).first()

            # Include jig-specific information (required fields for jig are always present)
            upj = part.custom_upj or (jig.maxUPJ if jig else 5)
            jpl = part.custom_jpl or (jig.maxJPL if jig else 10)
            mpj = part.custom_mpj or (jig.MPJ if jig else 2)
            jig_type = jig.jig_type if jig else "No Jig"

            # Load-independent operations (polishing and blasting)
            load_independent_operations = []
            if part.polishing_selection_status == 1:
                for step in polishing_details:
                    load_independent_operations.append({
                        "operation": f"Polishing; Step {step['step_number']}, Equipment: {step['equipment']}, Grit: {step['grit']}, Compound: {step['compound']}",
                        "duration": 4 * component_job.loads_required,  # Replace with actual logic if needed
                        "notes": f"DD/MM & Initial(s) & Quantity: {order_line.quantity}"
                    })

            if part.blasting_selection_status == 1:
                blasting_equipment = part.blasting
                load_independent_operations.append({
                    "operation": f"Blasting ({blasting_equipment})",
                    "duration": 2 * component_job.loads_required,  # Replace with actual logic if needed
                    "notes": f"DD/MM & Initial(s) & Quantity: {order_line.quantity}"
                })

            # Prepare the JSON response with all necessary details     
            data = {
                "component_job_id": component_job.component_job_id,
                "customer_name": component_job.customer_name,
                "purchase_order_number": order.purchase_order_number,
                "part_number": part.part_number,
                "part_description": part.part_description,
                "quantity": order_line.quantity,
                "required_jigs": component_job.required_jigs,
                "units_per_load": component_job.units_per_load,
                "quantity_of_final_load": component_job.quantity_of_final_load,
                "jig_type": jig_type,
                "upj": upj,
                "jpl": jpl,
                "loads_required": component_job.loads_required,
                "buzzbars_required": component_job.buzzbars_required,
                "load_independent_operations": component_job.load_independent_operations,
                "operations": component_job.operations,
                "voltage": part.voltage,
                "etch": part.etch,  
                "anodising_duration": part.anodising_duration,  
                "strip_etch": part.strip_etch,  
                "blasting": part.blasting,  
                "brightening": part.brightening, 
                "polishing": polishing_details,  
                "part_image_url": part.image,
                "jig_image_url": jig.image,
            }

            # Log detailed response data for debugging
            logger.info(f"Component job details fetched successfully: {data}")

            return jsonify(data), 200
        else:
            error_message = f"Component job with ID {component_job_id} not found."
            logger.warning(error_message)
            return jsonify({'error': error_message}), 404
    except Exception as e:
        # Log the error with stack trace for debugging
        logger.error(f"Error fetching component job details for ID {component_job_id}: {str(e)}", exc_info=True)
        return jsonify({'error': 'An unexpected error occurred. Please try again later.'}), 500

@app.route('/manage_customers', methods=['GET', 'POST'])
def manage_customers():
    logger.info("Accessed manage customers route")

    if request.method == 'POST':
        try:
            customer_name = request.form['customer_name']
            save_new_customer(customer_name)
            flash('Customer added successfully!', 'success')

            # ✅ Return JSON response for AJAX-based dynamic updates
            return jsonify({"success": True, "customer_name": customer_name})
        
        except Exception as e:
            log_exception(e, "Error in managing customers")
            return jsonify({"error": f"An error occurred: {str(e)}"}), 500

    try:
        # ✅ Fetch all customers normally for rendering the page
        customers = [dict(customer_id=str(c.customer_id), customer_name=c.customer_name) for c in Customer.query.all()]
        return render_template('manage_customers.html', customers=customers)

    except Exception as e:
        log_exception(e, "Error in loading customers")
        return render_template('error.html', error_message=f"An error occurred while loading customers: {e}")


# Save a new customer to the database
def save_new_customer(customer_name):
    logger.info(f"Saving new customer: {customer_name}")
    try:
        # Check if the customer already exists using customer_id if provided, else fallback to customer_name
        customer_id = request.form.get('customer_id')
        if customer_id:
            existing_customer = Customer.query.filter_by(customer_id=customer_id).first()
        else:
            existing_customer = Customer.query.filter_by(customer_name=customer_name).first()


        if existing_customer:
            flash(f'Customer "{customer_name}" already exists.', 'warning')
            logger.info(f'Customer "{customer_name}" already exists in the database.')
        else:
            # Add new customer
            new_customer = Customer(customer_name=customer_name)
            db.session.add(new_customer)
            db.session.commit()  # Commit the transaction
            logger.info(f'Successfully added customer: {customer_name}')
    except SQLAlchemyError as db_err:
        db.session.rollback()  # Rollback if there's a database  error
        logger.error(f"Database error occurred while saving customer: {db_err}")
        raise db_err
    except Exception as e:
        logger.error(f"An error occurred while saving customer: {e}")
        raise e

@app.route('/manage_parts', methods=['GET', 'POST'])
def manage_parts():
    logger.info("Accessed manage parts route")
    try:
        # Fetch all customers for the dropdown
        customers = Customer.query.all()

        # Get search parameters from the query string
        selected_customer_id = request.args.get('customer_id')
        search_query = request.args.get('search', '').lower()

        # Initialize parts query
        query = Part.query

        # Filter by selected customer, if applicable
        if selected_customer_id:
            query = query.filter_by(customer_id=selected_customer_id)

        # Filter by search query (part number or description)
        if search_query:
            query = query.filter(
                or_(
                    Part.part_number.ilike(f"%{search_query}%"),
                    Part.part_description.ilike(f"%{search_query}%")
                )
            )

        # Fetch the filtered parts
        parts = query.order_by(Part.part_number.desc()).all()

        # Handle POST requests for actions (upload or delete)
        if request.method == 'POST':
            action = request.form.get('action')
            part_number = request.form.get('part_number')

            if action == 'upload_image':
                # Handle image upload
                image_file = request.files.get('image_file')
                if image_file and image_file.filename:
                    # Validate file type
                    ALLOWED_EXTENSIONS = ['.png', '.jpg', '.jpeg', '.gif', '.bmp']
                    _, file_extension = os.path.splitext(image_file.filename)
                    if file_extension.lower() not in ALLOWED_EXTENSIONS:
                        flash("Invalid image file type. Allowed types: PNG, JPG, JPEG, GIF, BMP.", "danger")
                        return redirect(url_for('manage_parts'))

                    # Upload the image to Azure Blob
                    try:
                        image_url = upload_image_to_blob(
                            image_file=image_file,
                            part_number=part_number,
                            blob_service_client=BLOB_SERVICE_CLIENT,  # Pass this argument
                            blob_container="bamscontainer"           # Pass the correct container name
                        )
                        logger.info(f"Image uploaded successfully: {image_url}")

                        # Update the part with the image URL
                        part = Part.query.filter_by(part_number=part_number).first()
                        if not part:
                            flash(f"No part found with part number {part_number}.", "danger")
                            return redirect(url_for('manage_parts'))

                        part.image = image_url
                        db.session.commit()
                        flash("Image uploaded and part updated successfully!", "success")
                    except AzureError as e:
                        logger.error(f"Failed to upload image: {e}")
                        flash("Failed to upload image. Please try again.", "danger")
                    return redirect(url_for('manage_parts'))

            elif action == 'delete_part':
                part = Part.query.filter_by(part_number=part_number).first()
                if not part:
                    flash(f"No part found with part number {part_number}.", "danger")
                    return redirect(url_for('manage_parts'))

                # 🚀 Step 1: Delete Component Jobs related to this part
                component_jobs = ComponentJob.query.filter_by(part_id=part.part_number).all()
                for job in component_jobs:
                    db.session.delete(job)
                logger.info(f"Deleted {len(component_jobs)} component jobs for part {part.part_number}.")

                # 🚀 Step 2: Delete Order Lines related to this part
                order_lines = OrderLine.query.filter_by(part_number=part.part_number).all()
                for line in order_lines:
                    db.session.delete(line)
                logger.info(f"Deleted {len(order_lines)} order lines for part {part.part_number}.")

                # 🚀 Step 3: Delete the part
                db.session.delete(part)
                db.session.commit()
                
                flash(f"Part {part_number} and all related records deleted successfully!", "success")
                return redirect(url_for('manage_parts'))

        # Render the template with parts and customers
        return render_template(
            'manage_parts.html',
            parts=parts,
            customers=customers,
            selected_customer_id=selected_customer_id,
            search_query=search_query
        )

    except Exception as e:
        logger.exception("Error in managing parts")
        flash(f"An error occurred while managing parts: {e}", 'danger')
        return render_template('error.html')

@app.route('/jigs')
def jigs():
    logger.info("Accessed jigs route")
    try:
        # Load all jigs from the database using SQLAlchemy
        jigs = Jig.query.all()
        return render_template('jigs.html', jigs=jigs)
    except Exception as e:
        logger.exception("Error loading jigs")
        flash(f"An error occurred while loading jigs: {e}", 'danger')
        return render_template('error.html')
    
@app.route('/gantt_chart', methods=['GET'])
def gantt_chart():
    """Render the Gantt Chart page."""
    logger.info("Gantt Chart page loaded!")
    
    gantt_jobs = GanttJob.query.all()

    return render_template('gantt_chart.html', gantt_jobs=gantt_jobs)


# Route to fetch available component jobs
@app.route('/get_component_jobs', methods=['GET'])
def get_component_jobs():
    try:
        jobs = ComponentJob.query.all()
        job_list = [
            {
                "component_job_id": job.component_job_id,
                "customer_name": job.customer_name,
                "part_number": job.part_id,
                "loads_required": job.loads_required,
                "operations": job.operations,
            }
            for job in jobs
        ]
        return jsonify(job_list), 200
    except Exception as e:
        return jsonify({"error": f"Failed to load component jobs: {str(e)}"}), 500
    

# ✅ Safe JSON Parsing Helper
def parse_json_field(field):
    """Convert JSON string to Python list if needed"""
    if isinstance(field, str):  # If stored as a string, parse it
        return json.loads(field)
    return field  # If it's already a list, return as is

@app.route('/gantt_job', methods=['POST'])
def create_gantt_job():
    """
    Creates a Gantt job for a component job while ensuring:
    - Operations within a single job follow proper sequencing.
    - Loads within a job are scheduled sequentially (not in parallel).
    - Previous Gantt jobs do NOT influence new job start times.
    - Normalization logic for Cold Seal, Anodising, and Rinse steps is retained.
    """
    try:
        data = request.get_json()
        component_job_id = data.get("component_job_id")
        start_time = datetime.strptime(data.get("start_time"), "%Y-%m-%dT%H:%M")

                # ✅ Extracting these BEFORE they are used
        rinse_seal_route = data.get("rinse_seal_route", "default")
        anodising_tank = data.get("anodising_tank", "Anodising 1A")  # Default

        logger.info(f"🔍 Parsed selections -> Rinse/Seal: {rinse_seal_route}, Anodising Tank: {anodising_tank}")

        logger.info(f"🚀 Creating Gantt Job for Component Job ID {component_job_id} at {start_time}")

        # ✅ Fetch Component Job
        component_job = ComponentJob.query.get(component_job_id)
        if not component_job:
            return jsonify({"error": "Component job not found."}), 404

        order_line = OrderLine.query.get(component_job.order_line_id)
        order = Order.query.get(order_line.order_id) if order_line else None
        customer_id = order.customer_id if order else None

        if not customer_id:
            return jsonify({"error": "Customer ID is missing for this job. Cannot proceed."}), 400

        # ✅ Corrected Handling for Operations
        if isinstance(component_job.operations, str):
            operations = json.loads(component_job.operations)  # Deserialize if it's a string
        elif isinstance(component_job.operations, list):
            operations = component_job.operations  # It's already a list, so we use it directly
        else:
            operations = []

        if not operations:
            return jsonify({"error": "No valid operations found in the component job."}), 400

        # ✅ Normalization Logic: Standardize Naming for Certain Operations
        for operation in operations:
            if operation["operation"] in ["Cold Seal 30 min", "Cold Seal 15 min"]:
                operation["operation"] = "Cold Seal A"
            elif operation["operation"] == "Anodising":
                operation["operation"] = "Anodising 1A"  # Default to Anodising 1A
            elif operation["operation"] == "Water Rinse (1 or 2)":
                operation["operation"] = "Water Rinse 1"
            elif operation["operation"] == "Water Rinse (3 or 4)":
                operation["operation"] = "Water Rinse 3"
            elif operation["operation"] == "Water Rinse (5 or 6)":
                operation["operation"] = "Water Rinse 5"

        gantt_jobs = []
        last_load_end_time = start_time  # Tracks the last load's end time

        # ✅ Ensure loads are scheduled sequentially (not in parallel)
        for load_number in range(1, component_job.loads_required + 1):
            prev_end_time = last_load_end_time  # Start from the previous load's last operation

            gantt_job = GanttJob(
                component_job_id=component_job_id,
                order_id=order.order_id,
                customer_id=customer_id,
                load_number=load_number
            )

            # ✅ Assign Operations within the same job
            for operation in operations:
                operation_name = operation["operation"]
                duration = float(operation.get("duration", 0))

                column_mapping = COLUMN_MAPPING.get(operation_name)
                if column_mapping:
                    start_col, end_col = column_mapping
                    setattr(gantt_job, start_col, prev_end_time)
                    prev_end_time += timedelta(minutes=duration)  # Ensuring proper sequencing
                    setattr(gantt_job, end_col, prev_end_time)

            last_load_end_time = prev_end_time  # Update last load's end time for the next load
            db.session.add(gantt_job)

        # ✅ Commit Gantt Jobs for this Component Job
        db.session.commit()

        logger.info(f"✅ Gantt Job(s) created successfully for Component Job {component_job_id}")

        adjust_gantt_job_timestamps(component_job_id, rinse_seal_route, anodising_tank)

        return jsonify({"success": True}), 201

    except Exception as e:
        db.session.rollback()
        logger.error(f"❌ Error creating Gantt Job: {str(e)}")
        return jsonify({"error": str(e)}), 500
    
@app.route('/api/get_gantt_jobs', methods=['GET'])
def get_gantt_jobs():
    """API: Fetch a list of Gantt Jobs for the delete dropdown."""
    try:
        gantt_jobs = (
            db.session.query(GanttJob.gantt_job_id, GanttJob.component_job_id, Order.customer_id, 
                             Customer.customer_name, GanttJob.load_number)
            .join(Order, GanttJob.order_id == Order.order_id)
            .join(Customer, Order.customer_id == Customer.customer_id)
            .all()
        )

        job_list = [
            {
                "gantt_job_id": job.gantt_job_id,
                "component_job_id": job.component_job_id,
                "customer_name": job.customer_name,
                "load_number": job.load_number
            }
            for job in gantt_jobs
        ]

        return jsonify(job_list), 200

    except Exception as e:
        logger.error(f"❌ Error fetching Gantt Jobs: {str(e)}")
        return jsonify({"error": "Failed to load Gantt Jobs"}), 500

@app.route('/api/delete_gantt_job/<int:gantt_job_id>', methods=['DELETE'])
def delete_gantt_job(gantt_job_id):
    """Deletes a Gantt Job by ID, ensuring related data is fully loaded before deletion."""
    try:
        # ✅ Fetch Gantt Job with all necessary relationships loaded (prevents lazy load issues)
        gantt_job = db.session.query(GanttJob).options(
            joinedload(GanttJob.order)  # Load 'order' to prevent lazy-loading errors
        ).filter_by(gantt_job_id=gantt_job_id).first()

        if not gantt_job:
            return jsonify({"error": f"Gantt Job {gantt_job_id} not found"}), 404

        db.session.delete(gantt_job)  # ✅ Delete the job
        db.session.commit()  # ✅ Commit the deletion

        logger.info(f"✅ Gantt Job {gantt_job_id} deleted successfully.")
        return jsonify({"success": True, "message": f"Gantt Job {gantt_job_id} deleted"}), 200

    except Exception as e:
        db.session.rollback()
        logger.error(f"❌ Error deleting Gantt Job {gantt_job_id}: {str(e)}")
        return jsonify({"error": f"Failed to delete Gantt Job: {str(e)}"}), 500

@app.route('/api/shift_gantt_job/<int:gantt_job_id>', methods=['POST'])
def shift_gantt_job(gantt_job_id):
    """Shifts all timestamps for a Gantt Job by X minutes."""
    try:
        data = request.get_json()
        shift_minutes = int(data.get("shift_minutes", 0))  # Get shift value from request

        if shift_minutes == 0:
            return jsonify({"error": "No shift value provided"}), 400

        # ✅ Fetch the job with all timestamps
        gantt_job = db.session.query(GanttJob).options(
            joinedload(GanttJob.order)  
        ).filter_by(gantt_job_id=gantt_job_id).first()

        if not gantt_job:
            return jsonify({"error": "Gantt Job not found"}), 404

        shift_delta = timedelta(minutes=shift_minutes)

        # ✅ Iterate over all time-related columns and shift them
        for field in COLUMN_MAPPING.values():
            if isinstance(field, list):  # Handles multi-step operations
                for start_col, end_col in field:
                    start_time = getattr(gantt_job, start_col)
                    end_time = getattr(gantt_job, end_col)

                    if start_time and end_time:
                        setattr(gantt_job, start_col, start_time + shift_delta)
                        setattr(gantt_job, end_col, end_time + shift_delta)

        db.session.commit()  # ✅ Save the changes

        logger.info(f"✅ Gantt Job {gantt_job_id} shifted by {shift_minutes} minutes.")
        return jsonify({"success": True, "message": f"Job shifted by {shift_minutes} minutes"}), 200

    except Exception as e:
        db.session.rollback()
        logger.error(f"❌ Error shifting Gantt Job {gantt_job_id}: {str(e)}")
        return jsonify({"error": f"Failed to shift Gantt Job: {str(e)}"}), 500

    
@app.route('/gantt_data', methods=['GET'])
def get_gantt_data():
    try:
        # ✅ Optimize Query Performance: Use joinedload to fetch related data in a single query
        gantt_jobs = (
            GanttJob.query
            .options(
                joinedload(GanttJob.order).joinedload(Order.customer)  # Preload Order & Customer
            )
            .all()
        )

        if not gantt_jobs:
            return jsonify({"message": "No Gantt jobs available."}), 200

        job_data = []
        process_steps_list = list(COLUMN_MAPPING.keys())  # ✅ Extract clean process steps

        for job in gantt_jobs:
            order = job.order  # Preloaded Order
            customer = order.customer if order else None  # Preloaded Customer

            job_dict = {
                "component_job_id": job.component_job_id,
                "customer_id": order.customer_id if order else None,
                "customer_name": customer.customer_name if customer else "Unknown",
                "order_id": job.order_id,
                "load_number": job.load_number,
                "process_steps": {}
            }

            # ✅ Iterate over COLUMN_MAPPING to extract all timestamps
            for process_name, columns in COLUMN_MAPPING.items():
                process_timestamps = []  # Store all valid timestamps

                if isinstance(columns[0], list):  # Multi-option steps
                    for column_pair in columns:
                        start_column, end_column = column_pair
                        start_time = getattr(job, start_column, None)
                        end_time = getattr(job, end_column, None)

                        if start_time and end_time:
                            process_timestamps.append({
                                "start": start_time.isoformat(),
                                "end": end_time.isoformat()
                            })

                else:  # Standard single-step process
                    start_column, end_column = columns
                    start_time = getattr(job, start_column, None)
                    end_time = getattr(job, end_column, None)

                    if start_time and end_time:
                        process_timestamps.append({
                            "start": start_time.isoformat(),
                            "end": end_time.isoformat()
                        })

                # ✅ Store timestamps if any exist
                if process_timestamps:
                    job_dict["process_steps"][process_name] = process_timestamps

            job_data.append(job_dict)

        return jsonify({
            "jobs": job_data,
            "process_steps": process_steps_list
        }), 200

    except Exception as e:
        app.logger.error(f"❌ Error fetching Gantt data: {str(e)}", exc_info=True)
        return jsonify({"error": f"Failed to fetch Gantt data: {str(e)}"}), 500
    
def adjust_gantt_job_timestamps(component_job_id, rinse_seal_route, anodising_tank):
    """
    Adjusts Gantt Job timestamps based on user-selected rinse/seal route and anodising tank.
    This modifies all loads for the given component job.
    """
    try:
        logger.info(f"🟢 [Adjust Function] Triggered for Component Job ID: {component_job_id}")
        logger.info(f"🔍 Selected rinse/seal route: {rinse_seal_route}, Anodising Tank: {anodising_tank}")

        gantt_jobs = GanttJob.query.filter_by(component_job_id=component_job_id).all()
        if not gantt_jobs:
            logger.warning(f"⚠️ No Gantt Jobs found for Component Job ID {component_job_id}. Skipping adjustments.")
            return

        logger.info(f"🔄 Found {len(gantt_jobs)} Gantt Jobs for adjustment.")

        for job in gantt_jobs:
            logger.info(f"🔧 Adjusting Gantt Job ID {job.gantt_job_id}...")

            # ✅ Adjust Rinse Steps Based on Selection
            if rinse_seal_route == "even_rinse_cold_seal_b":
                logger.info(f"🔄 Moving rinse steps to even numbers for job {job.gantt_job_id}")

                job.water_rinse_2_start, job.water_rinse_2_end = job.water_rinse_1_start, job.water_rinse_1_end
                job.water_rinse_1_start, job.water_rinse_1_end = None, None

                job.water_rinse_4_start, job.water_rinse_4_end = job.water_rinse_3_start, job.water_rinse_3_end
                job.water_rinse_3_start, job.water_rinse_3_end = None, None

                job.water_rinse_6_start, job.water_rinse_6_end = job.water_rinse_5_start, job.water_rinse_5_end
                job.water_rinse_5_start, job.water_rinse_5_end = None, None

                job.cold_seal_b_start, job.cold_seal_b_end = job.cold_seal_a_start, job.cold_seal_a_end
                job.cold_seal_a_start, job.cold_seal_a_end = None, None

                logger.info(f"✅ Rinse steps and Cold Seal moved for job {job.gantt_job_id}")

            # ✅ Adjust Anodising Tanks Based on Selection
            anodising_map = {
                "Anodising 1B": ("anodising_1a_start", "anodising_1a_end", "anodising_1b_start", "anodising_1b_end"),
                "Anodising 2A": ("anodising_1a_start", "anodising_1a_end", "anodising_2a_start", "anodising_2a_end"),
                "Anodising 2B": ("anodising_1a_start", "anodising_1a_end", "anodising_2b_start", "anodising_2b_end"),
            }

            if anodising_tank in anodising_map:
                old_start, old_end, new_start, new_end = anodising_map[anodising_tank]

                setattr(job, new_start, getattr(job, old_start))  # Move start time
                setattr(job, new_end, getattr(job, old_end))      # Move end time
                setattr(job, old_start, None)                     # Clear old timestamps
                setattr(job, old_end, None)

                logger.info(f"✅ Anodising moved from {old_start} to {new_start} for job {job.gantt_job_id}")

        db.session.commit()
        logger.info(f"✅ Adjusted timestamps successfully for {len(gantt_jobs)} Gantt Jobs.")

    except Exception as e:
        db.session.rollback()
        logger.error(f"❌ Failed to adjust Gantt Job timestamps: {e}", exc_info=True)


# Error Handlers
@app.errorhandler(500)
def internal_error(error):
    logger.exception("An internal server error occurred.")
    return render_template('500.html'), 500

@app.errorhandler(Exception)
def handle_exception(e):
    # Log the full stack trace
    logger.exception("An exception occurred: %s", e)
    return render_template('error.html', error=str(e)), 500

