# Multi-Source Candidate Data Transformer
Open the [Multi-Source Candidate Data Transformer Web Application](https://multi-sourcecandidatedatatransforme.vercel.app).

A Python-based candidate data transformation pipeline that consolidates candidate information from multiple heterogeneous sources (CSV, ATS JSON, recruiter notes, and optional GitHub profile URL) into a unified canonical profile. The system performs source detection, extraction, normalization, duplicate merging, provenance tracking, confidence scoring, and configurable output generation. A web interface is provided for easy execution, while the project can also be run locally. For implementation details, architecture, pipeline design, schema, merge policy, and assumptions, please refer to the design document included in this repository.

The application utilizes a strict deterministic match-key clustering strategy via union-find over matching E.164 phones or lowercased email variations to reliably resolve identity duplicates across sources without risking fuzzy name collisions.

---

## Architecture and Data Pipeline Flow

1. **Intake / Detection:** Determines source context automatically via file signatures or explicit overrides (`csv`, `ats_json`, `github`, `resume`).


2. **Extraction & Normalization:** Functional layer parsing formats into isolated canonical entities, mapping country aliases to ISO-3166 alpha-2, matching phones to E.164, and deduplicating lists.


3. **Cross-Source Merging:** Clusters profiles on verified contact coordinates using a union-find algorithm. It resolves scalar properties via multi-source confidence algorithms and system hierarchy overrides (`ats_json > csv > github`).


4. **Reshaped Projection:** Projects canonical documents onto dynamically assigned runtime layout structures (`default_config.json`, `custom_config.json`) utilizing subset filtering or value criteria.

---

## Option 1: Use the Deployed Application

1. Open the deployed application:
```
https://multi-sourcecandidatedatatransforme.vercel.app

```
Open the [Multi-Source Candidate Data Transformer Web Application](https://multi-sourcecandidatedatatransforme.vercel.app).


2. Upload one or more supported input files from the repository (`samples/`) for testing.
Sample files provided:


* `samples/recruiter.csv` — Recruiter candidate dataset


* `samples/ats_blob.json` — ATS exported candidate records


* `samples/recruiter_notes.txt` — Unstructured recruiter notes


* `samples/resume_aarav_sharma_namesake.docx` — Sample candidate resume


* `samples/resume_rohan_verma.docx` — Sample candidate resume
*(The `gen_*.py` files are helper scripts used to generate sample resumes and are not required to run the application.)*




3. (Optional) Enter a GitHub profile URL.


4. Select the desired profile output format.


5. (Optional) Configure the output: Select fields to include, rename output labels, choose normalization options, enable/disable confidence or provenance, and choose missing-value behavior.


6. Click **Generate Candidate Profiles**.


7. Review the generated candidate profiles in the browser.



---

## Option 2: Run Locally

### Requirements

* Python 3.10+
* pip



### Setup and Installation Instructions

Follow these step-by-step instructions to isolate and execute the platform natively on your local architecture.

1. **Initialize Root Environment:** Open your terminal (Command Prompt or terminal emulator) and enter the cloned root workspace:
```bash
cd Multi-Source-Candidate-Data-Transformer

```


2. **Configure Virtual Environment Sandbox:** Establish a self-contained dependency framework to guarantee execution isolation:
```bash
# Initialize isolated sandbox directory structure
python -m venv venv

# Activate active framework thread (Windows OS)
.\venv\Scripts\activate

# Activate active framework thread (macOS / Linux OS)
source venv/bin/activate

```


3. **Deploy Platform Dependencies:** Install the pipeline core libraries and deep unstructured document processing extensions:
```bash
# Deploy manifest requirements layer
pip install -r requirements.txt

# Deploy file handlers and web architecture frameworks
pip install pdfplumber python-docx fastapi uvicorn

```



### Execution Profiles

#### Local Web Dashboard

The framework contains an integrated Vercel-ready FastAPI module incorporating an embedded single-allocation UI stream layer to bypass edge platform IO degradation.

1. Initialize the local asynchronous runtime server loop:
```bash
python -m uvicorn api.index:app --reload

```


2. Navigate your local web client browser to the exposure gateway:
```
http://127.0.0.1:8000

```


3. **Usage:** Drag and drop unstructured resume sheets (`.docx`) or tracking charts (`.csv`, `.json`) straight onto the ingestion portal to view consolidated outputs instantly.



#### CLI Pipeline Execution Engine

Execute localized parsing processes straight through standard terminal commands:

* **Complete Profile Extraction (Standard Mapping):**
```bash
python cli.py --input samples/recruiter.csv --input samples/ats_blob.json --config config/default_config.json --output out_default.json --verbose

```


* **Basic Profile Extraction (Custom Core Mapping):**
```bash
python cli.py --input samples/recruiter.csv --input samples/ats_blob.json --config config/custom_config.json --output out_custom.json

```


* **Unstructured Asset Processing Explicit Declaration Override:**
```bash
python cli.py --input samples/resume_rohan_verma.docx:resume --config config/default_config.json

```



### Sample Demonstration Execution

The repository contains sample inputs inside the `samples/` directory. Recommended demonstration:

* Upload `recruiter.csv`

* Upload `ats_blob.json`

* Upload `recruiter_notes.txt`

* (Optional) Enter a GitHub profile URL


* Click **Generate Candidate Profiles**


The application will automatically perform source detection, candidate information extraction, field normalization, duplicate merging, provenance tracking, confidence scoring, and configuration-driven candidate profile generation.

---

## Running Tests

Execute the comprehensive architectural regression testing platform to guarantee matching confidence thresholds and verify structural sanity rules locally:

```bash
python -m pytest tests/ -v

```
## Sample Output File

Available in the root directory: `out_default.json` and `out_custom.json`.

---




## Assumptions

- Phone numbers are normalized to E.164 format.
- Bare 10-digit numbers are treated as Indian (+91) numbers by default.
- Candidate matching is deterministic using email, phone, GitHub, and LinkedIn identifiers.
- Recruiter notes are used as optional enrichment data.

---

## Current Limitations

- OCR for scanned resumes is not implemented.
- Live LinkedIn profile ingestion is not supported because LinkedIn provides no public API and automated scraping violates their Terms of Service. LinkedIn profile URLs stored in CSV or ATS datasets are preserved in the output schema.
- Fuzzy or probabilistic candidate matching is not implemented.

---

## Author

Jaideep
Department of Computer Science and Engineering
NSUT, Delhi

```
