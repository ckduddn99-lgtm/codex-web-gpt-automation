from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPAT_PATH = ROOT / "bin" / "chatgpt_oracle_compat.py"


def load_compat():
    name = "chatgpt_oracle_compat_conversation_url_test"
    spec = importlib.util.spec_from_file_location(name, COMPAT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_current_oracle_drains_post_submit_conversation_url_monitor_before_shutdown() -> None:
    compat = load_compat()
    relative = "dist/src/browser/conversationUrlMonitor.js"

    assert relative in compat.PATCHES
    contract = compat.PATCHES[relative]
    assert contract["patch"] == "conversationUrlMonitor.drain-post-submit.patch"
    patch = compat.patch_root(compat.SUPPORTED_VERSION) / contract["patch"]
    text = patch.read_text(encoding="utf-8")
    assert "const pending = inFlight;" in text
    assert "await pending;" in text
    assert text.index("await pending;") < text.index("stopped = true;")

    index_contract = compat.PATCHES["dist/src/browser/index.js"]
    assert index_contract["patch"] == "browserIndex.await-post-submit-conversation-url.patch"
    index_patch = compat.patch_root(compat.SUPPORTED_VERSION) / index_contract["patch"]
    index_text = index_patch.read_text(encoding="utf-8")
    assert index_text.count("await conversationUrlMonitor?.update(") == 2
    assert "void conversationUrlMonitor?.schedule(\"post-submit\"" in index_text


def test_post_submit_binding_patches_ship_with_the_install_manifest() -> None:
    manifest = json.loads((ROOT / "install-manifest.json").read_text(encoding="utf-8"))
    files = set(manifest["include"])

    assert "bin/oracle-compat/0.18.0/conversationUrlMonitor.drain-post-submit.patch" in files
    assert "bin/oracle-compat/0.18.0/browserIndex.await-post-submit-conversation-url.patch" in files
