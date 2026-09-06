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
    # 가짜 Runtime은 호출 순번이 아니라 평가식 내용으로 프로브를 구분한다. preflight가
    # 표본을 몇 번 뜨든 테스트는 횟수가 아니라 계약을 검증한다.
    script = f"""
import {{ ensureModelSelection }} from {json.dumps(module_url)};
const makeRuntime = (apiStatuses, uiStatus) => {{
  const seen = {{ api: 0, ui: 0, selection: 0, submitted: false }};
  const Runtime = {{ evaluate: async ({{ expression }}) => {{
    if (expression.includes('accounts/check')) {{
      const index = Math.min(seen.api, apiStatuses.length - 1);
      seen.api += 1;
      return {{ result: {{ value: {{ status: apiStatuses[index] }} }} }};
    }}
    if (expression.includes('signedInSelectors')) {{
      seen.ui += 1;
      return {{ result: {{ value: {{ status: uiStatus }} }} }};
    }}
    if (/send-button|composer-submit|dispatchEvent/.test(expression)) {{ seen.submitted = true; }}
    seen.selection += 1;
    return {{ result: {{ value: {{ status: 'already-selected', label: 'GPT-5.6 Sol' }} }} }};
  }} }};
  return {{ Runtime, seen }};
}};
const run = async (apiStatuses, uiStatus) => {{
  const {{ Runtime, seen }} = makeRuntime(apiStatuses, uiStatus);
  try {{
    const value = await ensureModelSelection(Runtime, 'GPT-5.6 Sol', () => {{}}, 'select');
    return {{ ok: true, seen, value }};
  }} catch (error) {{
    return {{ ok: false, seen, message: error.message }};
  }}
}};
console.log(JSON.stringify({{
  authenticated: await run(['authenticated'], 'signed-in'),
  guestWithAnonymousUi: await run(['guest'], 'anonymous'),
  guestApiButSignedInUi: await run(['guest'], 'signed-in'),
  unknownApi: await run(['unknown'], 'unknown'),
  transientGuestThenAuthenticated: await run(['guest', 'authenticated'], 'anonymous'),
}}));
"""
    completed = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    observed = json.loads(completed.stdout)

    # 인증됐으면 재시도도 UI 프로브도 없이 곧장 selector로 간다.
    authenticated = observed["authenticated"]
    assert authenticated["ok"] is True
    assert authenticated["seen"]["api"] == 1
    assert authenticated["seen"]["ui"] == 0
    assert authenticated["value"]["status"] == "already-selected"
    assert authenticated["value"]["verified"] is True

    # 확실한 guest(API가 계속 guest + 익명 UI)에서만 실패한다. 모델 선택도 프롬프트
    # 전송도 시도하지 않는다 - 진짜 로그아웃 보호는 그대로 유지된다.
    guest = observed["guestWithAnonymousUi"]
    assert guest["ok"] is False
    assert "authentication is unavailable" in guest["message"]
    assert "model selection was not attempted" in guest["message"]
    assert guest["seen"]["api"] > 1, "단일 표본으로 중단하면 안 된다"
    assert guest["seen"]["ui"] == 1
    assert guest["seen"]["selection"] == 0
    assert guest["seen"]["submitted"] is False

    # 회귀 방지의 핵심: API가 guest처럼 보여도 UI가 로그인 상태면 죽이지 않는다.
    ambiguous = observed["guestApiButSignedInUi"]
    assert ambiguous["ok"] is True, "guest API 단독으로 정상 세션을 죽이면 안 된다"
    assert ambiguous["seen"]["ui"] == 1
    assert ambiguous["value"]["status"] == "already-selected"

    # non-2xx / 스키마 드리프트 / 부분 페이로드는 unknown이며 즉시 실패시키지 않는다.
    unknown = observed["unknownApi"]
    assert unknown["ok"] is True
    assert unknown["seen"]["ui"] == 0, "unknown은 UI 프로브까지 갈 필요가 없다"
    assert unknown["value"]["status"] == "already-selected"

    # 하이드레이션 도중의 일시적 guest 표본은 재시도가 구제한다 - 이번 회귀의 원인.
    transient = observed["transientGuestThenAuthenticated"]
    assert transient["ok"] is True
    assert transient["seen"]["api"] == 2
    assert transient["seen"]["ui"] == 0


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
