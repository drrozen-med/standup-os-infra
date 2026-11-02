import os

# tests never call the real model providers (llm.py)
os.environ["NAV_LLM"] = "off"
