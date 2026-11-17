import modal
import io
from studio_config import IMAGE_APP, HF_SECRET

app = modal.App(IMAGE_APP)

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "torch",
        "torchvision",
        "diffusers",
        "transformers",
        "accelerate",
        "sentencepiece",
        "protobuf",
        "safetensors"
    )
)

# FLUX.1-schnell is great for fast generation, or FLUX.1-dev. We will use schnell for speed.
MODEL_NAME = "black-forest-labs/FLUX.1-schnell"

@app.cls(gpu="A100", image=image, scaledown_window=300, secrets=[modal.Secret.from_name(HF_SECRET, required_keys=["HF_TOKEN"])])
class ImageWorker:
    @modal.enter()
    def setup(self):
        import os
        import torch
        from diffusers import FluxPipeline
        from huggingface_hub import login
        
        # Authenticate with HuggingFace using the secret injected by Modal
        hf_token = os.environ.get("HF_TOKEN")
        if hf_token:
            login(token=hf_token)
        else:
            print("WARNING: HF_TOKEN not found in environment.")
        
        print(f"Loading {MODEL_NAME}...")
        self.pipe = FluxPipeline.from_pretrained(
            MODEL_NAME, 
            torch_dtype=torch.bfloat16
        )
        self.pipe.enable_model_cpu_offload()
        print("Model loaded.")

    @modal.method()
    def generate(self, prompt: str, width: int = 1280, height: int = 720) -> bytes:
        import torch
        
        # Schnell needs very few steps (e.g. 4)
        image = self.pipe(
            prompt,
            width=width,
            height=height,
            guidance_scale=0.0,
            num_inference_steps=4,
            max_sequence_length=256,
            generator=torch.Generator("cpu").manual_seed(0)
        ).images[0]
        
        # Convert PIL image to bytes
        img_byte_arr = io.BytesIO()
        image.save(img_byte_arr, format='PNG')
        return img_byte_arr.getvalue()

@app.local_entrypoint()
def test_image():
    prompt = "A cinematic shot of a dark haunted house in the woods"
    worker = ImageWorker()
    print(f"Generating image for: {prompt}")
    img_bytes = worker.generate.remote(prompt)
    with open("test_flux.png", "wb") as f:
        f.write(img_bytes)
    print("Saved to test_flux.png")
