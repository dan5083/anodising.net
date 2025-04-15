Anodising.net
Track anodising orders and production jobs.
Generate Gantt schedules.
Handle customer and part management.

Anodising.net is a full-stack operations platform designed specifically for anodising businesses. Built with real-world industrial workflows in mind, it supports everything from intake and pricing to process scheduling and completion—streamlining production while maintaining full traceability.

🔧 Tech Stack
Backend: Flask + SQLAlchemy (ORM)

Database: SQL Server

Storage: Azure Blob Storage for images and documents

Hosting: Fully deployed on Microsoft Azure

ORM Integrations: Hybrid properties, JSON fields, constraints, and relationship mapping via SQLAlchemy

✅ Key Features
Order & Job Management:
Input orders, define parts, attach images, track by customer.

Gantt Scheduling:
Auto-generates detailed job breakdowns per part, including process-specific durations, rinse/dye/etch/pack operations, and load balancing logic.

Component Job Engine:
Dynamically creates multi-load process plans, complete with resource estimation (e.g., jig use, buzzbars, operators).

Custom Logic for Anodising:
Part-specific operations like polishing, blasting, sealing, and dye categories (in-line vs off-line) are intelligently handled.

Load-Aware Estimates:
Units per jig/load, final load adjustments, operator time allocation—all calculated in real time.

🚀 Running the App
Anodising.net is fully cloud-ready and was previously deployed entirely within Azure, using Blob Storage, App Service, and SQL Server.
To replicate or build upon this project:

Clone the repository

Set up environment variables for Azure credentials and PostgreSQL
