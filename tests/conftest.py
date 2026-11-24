"""Tests are deterministic and offline: the LLM module is switched off unless a test turns it on explicitly."""
import os

os.environ["SEE_LLM"] = "off"
