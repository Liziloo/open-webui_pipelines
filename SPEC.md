# System Spec

## Zone A: Open WebUI + chat‑mode (mini‑PC)

- Handles uploads, shows images, lets you tweak settings.
- Talks to a small **pipeline service** on the mini‑PC.

## Zone B: Pipeline Service (mini‑PC)

- Reads `batch_job.json` or single‑page requests.
- Runs policy logic, file I/O, and calls Ollama/Comfy/Kraken/Doctr.
- Writes `page_state.json`, `batch_summary.json`, and page artifacts.
- Stores everything as files in `/data` with directory structure described below.

## Zone C: ComfyUI, Ollama, Kraken, Doctr (GPU box)

- ComfyUI runs image preprocessing.
- Ollama runs the LLM models (Vision for triage, Text for iteration).
- Kraken / Doctr run OCR and produce metrics.

### Directory Trees

_Largely static files can be stored on HDD pool directory mounted to the computer with the large GPU via NFS_

```txt
/mnt/data
├── projects
│   ├── 1890_us_census_ohio
│   │   ├── images
│   │   │   ├── oh_cen1890_p0001.tif
│   │   │   └── ...
│   │   └── processed
│   │       ├── pages
│   │       │   ├── abc123
│   │       │   │   ├── original.tif
│   │       │   │   ├── pass0_comfy.png
│   │       │   │   ├── pass0_ocr.json
│   │       │   │   └── ...
│   │       ├── summary
│   │       │   ├── batch_summary.json
│   │       │   └── pages_to_reprocess.txt
│   │       └── batch_job.json
│   ├── 1920_birth_records_germany
│   ├── 1850_will_and_estate_ky
```

_Files that need faster and more frequent access by the system get stored on SSD_

```txt
/app
├── openwebui
├── pipelines/historic_image_cleaner
│   ├── pipeline.log
│   ├── comfy_workflows
│   │   └── archival_ocr_prep.json
│   ├── batch_job.json
│   └── ops_catalog.json
│   ├── active_sessions
│   │   └── abc123_page_state.json
│   └── batch_metadata
│       └── batch_2026_03_18_census_1890
│           ├── page_state.json
│           └── workflow_profiles.json
│   └── temp
│       └── comfy_input
│       └── ocr_input
│       └── ocr_output
```

## Data Model & Schemas

### 1. The Ops Catalog (`ops_catalog.json`)

```json
{
  "operations": {
    "geometry_transform": {
      "op_id": "transform",
      "description": "Handles spatial corrections including margin padding, sub-degree deskewing, and resolution upscaling to normalize the document canvas for the OCR engine.",
      "active_switch_node": "20",
      "nodes": {
        "pad": {
          "node_id": "5",
          "inputs": {
            "left": { "type": "int", "range": [0, 1024], "step": 8 },
            "top": { "type": "int", "range": [0, 1024], "step": 8 },
            "right": { "type": "int", "range": [0, 1024], "step": 8 },
            "bottom": { "type": "int", "range": [0, 1024], "step": 8 }
          }
        },
        "rotate": {
          "node_id": "29",
          "inputs": {
            "rotation_angle": { "type": "float", "range": [0.0, 359.9] }
          }
        },
        "upscale": {
          "node_id": "7",
          "inputs": {
            "upscale_method": {
              "type": "string",
              "options": [
                "nearest-exact",
                "bilinear",
                "area",
                "bicubic",
                "lanczos"
              ]
            },
            "scale_by": { "type": "float", "range": [0.01, 8.0] }
          }
        }
      }
    },
    "frequency_separation": {
      "op_id": "frequency",
      "description": "Isolates high-frequency text elements from low-frequency background noise by calculating a background map and subtracting it from the source to neutralize stains and parchment texture.",
      "active_switch_node": "21",
      "nodes": {
        "blur": {
          "node_id": "3",
          "inputs": {
            "blur_radius": { "type": "int", "range": [1, 31] },
            "sigma": { "type": "float", "range": [0.1, 10.0] }
          }
        },
        "blending_mode": {
          "node_id": "4",
          "inputs": {
            "blend_percentage": { "type": "float", "range": [0.0, 1.0] }
          }
        }
      }
    },
    "noise_reduction": {
      "op_id": "denoise",
      "description": "Applies non-linear smoothing via median filtering to suppress salt-and-pepper artifacts and foxing while maintaining sharp character edges.",
      "active_switch_node": "27",
      "nodes": {
        "median_filter": {
          "node_id": "25",
          "inputs": {
            "diameter": { "type": "int", "range": [0, 255] },
            "sigma_color": { "type": "float", "range": [-255.0, 255.0] },
            "sigma_space": { "type": "float", "range": [-255.0, 255.0] }
          }
        }
      }
    },
    "adaptive_threshold": {
      "op_id": "threshold",
      "description": "The final binarization stage; converts processed imagery into high-contrast 1-bit black and white data to maximize character recognition accuracy.",
      "active_switch_node": "22",
      "nodes": {
        "threshold": {
          "node_id": "2",
          "inputs": {
            "threshold": { "type": "float", "range": [0.0, 1.0] }
          }
        }
      }
    }
  }
}
```

### 2. The Page State (`page_state.json`)

```json
{
  "page_metadata": {
    "page_id": "abc123",
    "project_id": "1890_us_census_ohio",
    "source_path": "/mnt/data/projects/1890_us_census_ohio/images/p0001.tif",
    "created_at": "2026-03-19T11:40:00Z"
  },
  "processing_status": {
    "current_pass": 2,
    "max_passes": 5,
    "is_converged": false,
    "final_ocr_confidence": 0.0
  },
  "history": [
    {
      "pass_index": 0,
      "timestamp": "2026-03-19T11:41:00Z",
      "ops_applied": [],
      "ocr_results": {
        "engine": "doctr",
        "confidence_avg": 0.35,
        "word_count": 42,
        "metrics_path": "/mnt/data/projects/1890_us_census_ohio/processed/pages/abc123/pass0_ocr.json"
      },
      "artifact_path": "pass0_original.png"
    },
    {
      "pass_index": 1,
      "timestamp": "2026-03-19T11:42:30Z",
      "ops_applied": [
        {
          "op_id": "threshold",
          "params": { "threshold": 0.45 }
        }
      ],
      "ocr_results": {
        "engine": "doctr",
        "confidence_avg": 0.62,
        "word_count": 115,
        "metrics_path": "/mnt/data/projects/1890_us_census_ohio/processed/pages/abc123/pass1_ocr.json"
      },
      "artifact_path": "pass1_thresholded.png"
    }
  ],
  "policy_notes": "Last OCR pass showed significant improvement in word count but low character confidence. Suggesting noise reduction to clear background speckling."
}
```

### 3. The Batch Job (`batch_job.json`)

```json
{
  "batch_metadata": {
    "batch_id": "batch_2026_03_18_census_ohio",
    "project_name": "1890_us_census_ohio",
    "priority": 1,
    "notes": "Archival census scans from Hamilton County. High frequency of bleed-through expected."
  },
  "input_output": {
    "input_directory": "/mnt/data/projects/1890_us_census_ohio/images/",
    "output_base_directory": "/mnt/data/projects/1890_us_census_ohio/processed/",
    "file_pattern": "*.tif"
  },
  "runtime_constraints": {
    "max_passes_per_page": 5,
    "min_confidence_threshold": 0.85,
    "ocr_engine": "doctr",
    "policy_model": "llama3-vision",
    "timeout_per_page_seconds": 120
  },
  "default_ops": [
    {
      "op_id": "threshold",
      "params": { "threshold": 0.5 }
    }
  ],
  "batch_status": {
    "state": "active",
    "started_at": "2026-03-19T10:00:00Z",
    "completed_pages": 45,
    "total_pages": 1200,
    "failed_pages": 2
  }
}
```

---

## Communication Logic (The "Bridges")

### Zone B $\rightarrow$ Zone C (The Request)

When the **Pipeline Service (Zone B)** executes a pass, it targets the **GPU Box (Zone C)** endpoints:

- **POST /comfy_execute:** Sends the `archival_ocr_prep.json` workflow with parameters injected from the current `page_state`.
- **POST /ocr_process:** Sends the path of the generated `passN_comfy.png` and receives the `ocr_metrics.json`.

### Zone B $\rightarrow$ Ollama (The Policy)

The "Single Glance" rule ensures the Vision-LLM only analyzes the image once at the start.

1.  **Pass 0 (Vision Triage):** The Pipeline sends the `original.tif`.

    > "Analyze this document. Categorize the physical degradation (faded, skewed, noise). Provide an initial strategy based on the `ops_catalog`."
    - Result is saved to `policy_notes`.

2.  **Pass 1+ (Iterative Feedback):** The Pipeline sends **only text**.
    > "Initial Triage: [policy_notes]. Current OCR confidence is 0.62. Applied Ops in Pass 1: [history[1]]. Based on the `ops_catalog`, return a JSON array of recommended adjustments to reach the threshold."

---
