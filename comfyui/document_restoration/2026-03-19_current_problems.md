### 1. Architectural & Logical "Nonsense"

- **Destructive Image Iteration:** In your `pipe` loop, you pass `current_bytes` into `process_in_comfyui`, and then overwrite `current_bytes` with the output. On Pass 2, you are feeding the _already processed_ image back into ComfyUI to be processed again. Image restoration parameters (like thresholding and denoising) should be applied to the _original_ image on each iterative tweak, not compounded on top of previous outputs, which will quickly destroy the image data.
- **Fake OCR Integration:** The `SPEC.md` explicitly states that Zone B calls Kraken/Doctr via a `POST /ocr_process` endpoint to get `ocr_metrics.json`. However, the pipeline code completely fakes this with `current_confidence += 0.3 # Mock OCR increment`. It never actually evaluates the text.
- **Ignoring the Ops Catalog:** The spec emphasizes the `ops_catalog.json` as the source of truth for operations, node IDs, and parameters. The pipeline code ignores this file entirely and hardcodes the prompt instructions, node IDs, and parameter ranges directly into the Python script.
- **Ignoring Batch Constraints:** The spec mentions `batch_job.json` contains runtime constraints like `max_passes_per_page` and `min_confidence_threshold`. The pipeline hardcodes these as `MAX_ITERATIONS = 3` and `current_confidence < 0.9` instead of reading the configuration.

### 2. JSON Schema & Dictionary Mismatches

- **Transform vs. Geometry:** In `get_initial_triage`, you explicitly instruct the LLM to return a JSON key called `"transform"`. However, in `process_in_comfyui`, your code looks for `report.get("geometry", {})`. Because of this mismatch, geometry/transform settings will _never_ be applied.
- **Threshold vs. Level:** In the LLM prompt, you ask for `"threshold": {"active": bool, "threshold": float}`. But in `process_in_comfyui`, you attempt to read `thresh.get("level", 0.20)`. It will always fail to find "level" and default to 0.20.

### 3. ComfyUI Integration Errors

- **Wrong Node IDs:** Your code modifies ComfyUI nodes that contradict your `SPEC.md`:
  - _Denoise:_ Spec says Node 25. Code modifies Node 26 (`workflow["26"]["inputs"]["sigma_color"]`).
  - _Rotation:_ Spec says Node 29. Code modifies Node 6 (`workflow["6"]["inputs"]["rotation"]`).
- **Brittle Polling & Hardcoded Output Node:** The polling loop checks if `prompt_id` is in the `/history` endpoint and blindly assumes the output is in node `"23"`. If the workflow fails, it will never appear in history, causing the pipeline to hang until the 90-second timeout. Furthermore, if the save node isn't exactly "23", it will crash.
- **Broken Image Retrieval:** To fetch the image, you call `requests.get(f"{self.valves.COMFYUI_URL}/view?filename={out}")`. ComfyUI's `/view` endpoint typically requires `subfolder` and `type` parameters (e.g., `type=output`) to locate the file correctly. Without them, it may return a 404.

### 4. File System & Path Incompatibilities

- **Workflow JSON Path:** `Valves.WORKFLOW_JSON_PATH` points to `/app/backend/data/document_restoration_flow.json`. The spec states the workflow is located at `/app/pipelines/historic_image_cleaner/comfy_workflows/archival_ocr_prep.json`.
- **Base Data Directory:** `Valves.BASE_DATA_DIR` points to `/mnt/data/projects/open_webui_chats`. The spec defines the structure starting at `/mnt/data/projects/` (e.g., `/mnt/data/projects/1890_us_census_ohio/...`).
- **Temp Directory Structure:** `Valves.TEMP_DIR` points to `/app/pipelines/historic_image_cleaner/temp`. The spec defines subdirectories like `temp/ocr_input` and `temp/comfy_input` which are not being used or created.

### 5. Minor Bugs & Formatting Errors

- **Markdown Image MIME Type:** When yielding the final image to the chat UI, you format the markdown as `![Pass...](data:image/jpeg;base64,{img_b64})`. However, the bytes you are encoding come directly from ComfyUI, which outputs PNGs. It should be `image/png`.
- **LLM JSON Mode Risk:** In `get_next_adjustment`, you set `"format": "json"` but you don't provide the LLM with the strict JSON schema in the prompt like you did in the first pass. Ollama's JSON mode requires the schema to be explicitly stated in the prompt, otherwise it may return unpredictable JSON structures that will break your `process_in_comfyui` parsing.
