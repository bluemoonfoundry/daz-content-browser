"""Standalone smoke-test: load the ONNX embedding model and verify output shape."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from embedding_utils import load_embedding_model, generate_embeddings

load_embedding_model()

text = "A beautiful female elf warrior in a forest"
vec = generate_embeddings(text)

print(f"Shape : {vec.shape}")
print(f"Dtype : {vec.dtype}")
print(f"First 5 values: {vec[:5]}")

assert vec.shape == (1024,), f"Expected (1024,), got {vec.shape}"
print("\nPASS — shape is (1024,)")
