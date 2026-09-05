from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "bin" / "chatgpt_oracle_compat.py"


def load_compat():
    name = "chatgpt_oracle_model_auth_compat_test"
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_published_0180_model_selection_fails_closed_for_guest_profile(tmp_path: Path) -> None:
    compat = load_compat()
    configured = os.environ.get("ORACLE_018_PACKAGE_ROOT", "").strip()
    source = Path(configured) if configured else Path("__oracle_018_cache_unset__")
    if not source.is_dir():
        if os.environ.get("CI"):
            pytest.fail("CI must prepare the exact published Oracle 0.18.0 package")
        pytest.skip("published Oracle 0.18.0 package root is unavailable")
    package = tmp_path / "oracle-model-auth"
    shutil.copytree(source, package)
    result = compat.ensure_oracle_compatibility(
        "oracle 0.18.0",
        package_root=package,
        backup_root=tmp_path / "backup",
    )
    relative = "dist/src/browser/actions/modelSelection.js"
    assert relative in set(result["changed"]) | set(result["already_patched"])
    assert compat.sha256_file(package / relative) == compat.PATCHES[relative]["patched"]

    node = shutil.which("node")
    assert node is not None
    module_url = (package / relative).resolve().as_uri()
    script = f"""
import {{ ensureModelSelection }} from {json.dumps(module_url)};
const run = async (authStatus) => {{
  let calls = 0;
  const Runtime = {{ evaluate: async () => {{
    calls += 1;
    if (calls === 1) return {{ result: {{ value: {{ status: authStatus }} }} }};
    return {{ result: {{ value: {{ status: 'already-selected', label: 'GPT-5.6 Sol' }} }} }};
  }} }};
  try {{
    const value = await ensureModelSelection(Runtime, 'GPT-5.6 Sol', () => {{}}, 'select');
    return {{ ok: true, calls, value }};
  }} catch (error) {{
    return {{ ok: false, calls, message: error.message }};
  }}
}};
console.log(JSON.stringify({{ guest: await run('guest'), authenticated: await run('authenticated') }}));
"""
    completed = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    observed = json.loads(completed.stdout)
    assert observed["guest"]["ok"] is False
    assert observed["guest"]["calls"] == 1
    assert "authentication is unavailable" in observed["guest"]["message"]
    assert "model selection was not attempted" in observed["guest"]["message"]
    assert observed["authenticated"]["ok"] is True
    assert observed["authenticated"]["calls"] == 2
    assert observed["authenticated"]["value"]["status"] == "already-selected"
    assert observed["authenticated"]["value"]["verified"] is True


def test_published_0180_model_selection_recognizes_advanced_composer_pill(tmp_path: Path) -> None:
    compat = load_compat()
    configured = os.environ.get("ORACLE_018_PACKAGE_ROOT", "").strip()
    source = Path(configured) if configured else Path("__oracle_018_cache_unset__")
    if not source.is_dir():
        if os.environ.get("CI"):
            pytest.fail("CI must prepare the exact published Oracle 0.18.0 package")
        pytest.skip("published Oracle 0.18.0 package root is unavailable")
    package = tmp_path / "oracle-model-advanced-pill"
    shutil.copytree(source, package)
    compat.ensure_oracle_compatibility(
        "oracle 0.18.0",
        package_root=package,
        backup_root=tmp_path / "backup-advanced-pill",
    )
    source_text = (package / "dist/src/browser/actions/modelSelection.js").read_text(encoding="utf-8")
    assert "const rawLabel =" in source_text
    assert "const advancedPillTokens = [" in source_text
    assert "'advanced'" in source_text
    assert "'intelligence'" in source_text
    assert "advancedPillTokens.some((token) => rawLabelLower.includes(token))" in source_text
    assert "const MODEL_BUTTON_WAIT_MS = 8000;" in source_text
    assert "assertResolvedModelSelection(desiredModel, observedLabel);" in source_text
