import modal
from studio_config import LLM_APP, HF_SECRET

app = modal.App(LLM_APP)

# Define the image with vLLM for fast inference
image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install("vllm", "huggingface_hub")
    .env({
        "VLLM_USE_FLASHINFER_SAMPLER": "0",
        "VLLM_USE_V1": "0",
        "VLLM_USE_DEEP_GEMM": "0"
    })
)

# Use a strong Qwen model as recommended. 
# Qwen2.5-7B-Instruct fits nicely on an A10G and generates excellent JSON.
MODEL_NAME = "Qwen/Qwen2.5-7B-Instruct"

@app.cls(gpu="A10G", image=image, scaledown_window=300, secrets=[modal.Secret.from_name(HF_SECRET, required_keys=["HF_TOKEN"])])
class LLMWorker:
    @modal.enter()
    def setup(self):
        import os
        from vllm import LLM
        from huggingface_hub import login
        
        # Authenticate with HuggingFace using the secret injected by Modal
        hf_token = os.environ.get("HF_TOKEN")
        if hf_token:
            login(token=hf_token)
        else:
            print("WARNING: HF_TOKEN not found in environment.")
            
        # Load the model into VRAM when the container starts
        print(f"Loading {MODEL_NAME}...")
        self.llm = LLM(model=MODEL_NAME, gpu_memory_utilization=0.9)
        print("Model loaded.")

    @modal.method()
    def generate(self, prompt: str, system_prompt: str) -> str:
        from vllm import SamplingParams
        
        # Guide the model to output JSON based on the system prompt
        sampling_params = SamplingParams(
            temperature=0.7, 
            max_tokens=4096,
            stop=["<|im_end|>"]
        )
        
        # Format conversation for Qwen
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ]
        
        outputs = self.llm.chat(messages, sampling_params=sampling_params)
        generated_text = outputs[0].outputs[0].text
        
        # Sometimes open weight models wrap JSON in markdown block: ```json ... ```
        # We can clean it up here or in the provider.
        return generated_text

@app.local_entrypoint()
def test_llm():
    system = "You are a knowledgeable assistant. Please provide the output in valid JSON format."
    prompt = "Give me a story of prpophet mohammad sm in JSON format with 3 paragraphs."
    worker = LLMWorker()
    print("Generating...")
    result = worker.generate.remote(prompt, system)
    print("Result:", result)
