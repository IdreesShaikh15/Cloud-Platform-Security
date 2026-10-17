# The dataset: Synthea synthetic patients

**Every patient in this project is invented.** The records come from **Synthea**, the open-source synthetic patient generator
(<https://github.com/synthetichealth/synthea>, Apache-2.0, maintained by MITRE). No real person's data is used anywhere: not in the repository, not in the
images, not on the cluster, not in the evaluation. Every service answer says `"source": "Synthea"` and `"synthetic": true`, the Patient Portal page says "Synthea
synthetic data only: these are invented patients", and the dashboard says "SYNTHETIC DATA ONLY" on every page.

## 1. Where the data comes from

| | |
|---|---|
| Generator | Synthea, release `master-branch-latest`, build `d9d07a6` (`synthea-with-dependencies.jar`, downloaded from the project's GitHub releases) |
| Command | `java -jar synthea-with-dependencies.jar -p 200 -s 20261016 -cs 20261016 -r 20261016 --exporter.csv.export=true --exporter.fhir.export=false Massachusetts` |
| Seed | 20261016 (patients and clinicians), reference date 2026-10-16: the same command gives the same patients |
| What Synthea produced | 240 patients (the 200 asked for that are alive, plus deceased ones Synthea generates along the way), as CSV: patients, conditions, observations, medications, encounters, claims, ... |
| What is kept (`scripts/gen-synthea-dataset.py`) | the **200 living** patients; per patient a medical record number (`SYN-` + 8 hex characters of Synthea's id), name, birth date, sex, city and state; the **active clinical disorders**; the **12 most recent laboratory results** (newest value per kind of test, with LOINC code, value and unit); the **active medications** (RxNorm code, start date, reason) |
| What is deliberately dropped | social security numbers, driver's licence and passport numbers, street addresses, coordinates, income, costs and claims, deceased patients, social-history "findings" and administrative "situations" (even though Synthea invents all of them) |
| Result | `apps/src/data/synthea-sample.json`: 200 patients, 491 conditions, 2,359 laboratory results, 494 medications, 431 KiB, committed to the repository and baked into the application image |

Synthea's names carry numeric suffixes (for example "Ted955 Yundt842"); they are kept on purpose, so nobody can mistake a record for a real person.
To regenerate: run the command above and then `python3 scripts/gen-synthea-dataset.py <synthea-output>/csv`; `tests/test_dataset.py` checks the committed file (labels, no identifiers, bounds).

## 2. How the data flows, in real time

```
 client pod                  patient-portal              records-api ──┐
 3 simulated clinicians  ──►  /api/login                              ├──► database (holds the dataset)
 + availability probe         /api/patients, /api/patient             │      /query /patient /labs /medications
 (a request every 1-3 s       /api/labs, /api/medications  ──► lab-results ──┤
  per clinician)                       │                      medication-service ┘
                                       └──► auth-service (login, token check on every protected call)
```
* **Only the database holds the dataset.** The Laboratory Results Service, the Medication Service and the EHR API read it from the database over HTTP, each behind a token check with the Login & Access Service; the Patient Portal calls them. This is the dependency graph the dashboard shows ("what isolating each service means").
* **Continuous, realistic traffic.** The client runs three simulated clinicians (`dr.smith`, `nurse.jones`, `dr.patel`) and the original once-a-second availability probe. Each clinician logs in (and again every 90-180 s; 5 % of logins are a mistyped password, so a failed login appears now and then), then looks at patient lists, opens a record and, often, its laboratory results and medications, with 1-3 seconds between actions. Measured on a test machine: about 3 requests per second reach the database, 0 errors, every answer under 10 KB (answers are capped at 50 rows).
* **Reads only.** Nothing writes to the dataset; the data is static, so "real time" means real, continuous *use* and real, continuous *monitoring* of the services, not new patients appearing.
* **The agents poll each workload once a second** (`/health` and `/_telemetry`) and the dashboard keeps the last five minutes of what they measured: requests per second, established outbound connections, data sent, and failed logins in the last 10 seconds. The Live Monitoring page draws one small chart for each of these for each service, labelled `LIVE_CLUSTER`, with the telemetry age ("STALE" after 10 s). These charts exist only on the Live page and only from the Live channel: the Demo channel never serves them, so no simulated reading can appear there.

## 3. Why real patient data is not used

* **Privacy and law.** Real patient records are protected health information. Using them for a student/demonstration platform would need consent, an ethics approval and a data-protection agreement, and a security demonstration that deliberately attacks workloads must never involve data whose exposure would harm a person.
* **The platform itself attacks and isolates workloads in demonstrations.** A copy of a "stolen" record must be harmless by construction.
* **Realistic is enough.** Synthea models whole patient lives with published clinical disease modules (hypertension, diabetes, anaemia, heart disease, ...), so the structure, the codes (SNOMED, LOINC, RxNorm) and the shape of the data match what a real hospital system holds, without a real person behind any record.
* **Reproducible.** A fixed seed and a documented converter mean anyone can regenerate the same file.
Limits of "realistic": Synthea is a model, not clinical truth. Values are plausible, not medically meaningful, and the sample is small (200 patients). It must not be used for clinical conclusions or for training anything meant to treat real patients.

## 4. What the agents see (and never see)

The security agents monitor **behaviour**, never content:
* from each workload they read `/health` and `/_telemetry`: process list, number of established outbound connections, bytes sent, file hashes, the count of failed logins per source address, and a **single counter of requests served**. The telemetry has no field that can hold a record (`tests/test_dataset.py` checks that no patient name, MRN or lab code appears in it);
* their Kubernetes permissions (`k8s/resilience/00-rbac.yaml`, checked by `scripts/verify-rbac.sh`) cannot read the application's data: no secrets, no exec into pods, only the isolation policy and the recovery fields of the workloads, plus pod listing and logs for the evidence snapshot. The services log nothing about requests, so a snapshot contains no records;
* **the dataset file is part of the known-good fingerprints.** The file-integrity rule hashes everything under `/app`, including `data/synthea-sample.json`, so a changed or replaced dataset is detected like a changed program file. After rebuilding the images (`scripts/build-images.sh`) the manifest lists five files: `app.py`, `data/synthea-sample.json`, `static/TRIPWIRE-TEST.txt`, `static/index.html`, `telemetry.py`; `scripts/verify-baseline.py` must print five-file matches for every workload, otherwise the agents would see a healthy workload as tampered (the upgrade script refuses to start the agents until it does).

## 5. Limitations (honest)

* A sample of 200 patients, static, read-only; no writes, no real clinical workflow.
* The traffic is a model of staff behaviour (three clinicians, a fixed mix of actions), not a recording of a hospital; rates are a few requests per second.
* The charts show what the agents measured; with a failed or stale agent they say "not reported" or "STALE" rather than inventing a value.
* Synthea names, dates and values are invented; any resemblance to a real person is coincidence.
