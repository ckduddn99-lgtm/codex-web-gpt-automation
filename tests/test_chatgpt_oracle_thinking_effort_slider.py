from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "bin" / "chatgpt_oracle_compat.py"
THINKING_TIME = "dist/src/browser/actions/thinkingTime.js"
LEVELS = ("light", "standard", "extended", "extra-high", "heavy", "pro")


def load_compat():
    name = "chatgpt_oracle_thinking_effort_slider_compat_test"
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_thinking_time_contract_ships_every_patch_it_names() -> None:
    """설치본에 없는 패치를 계약이 가리키면 클론에서만 동작한다."""
    compat = load_compat()
    contract = compat.PATCHES[THINKING_TIME]
    manifest = set(json.loads((ROOT / "install-manifest.json").read_text(encoding="utf-8"))["include"])
    named = [contract["patch"], contract["legacy_patch"], *contract["legacy_patches"].values()]
    for name in named:
        path = compat.patch_root(compat.SUPPORTED_VERSION) / name
        assert path.is_file(), f"{name} is named by the contract but missing on disk"
        relative = path.resolve().relative_to(ROOT).as_posix()
        assert relative in manifest, f"{relative} is named by the contract but not installed"


def test_thinking_time_contract_retires_the_pro_only_slider_level() -> None:
    """Pro 전용 판을 legacy로 인정해야 이미 패치된 설치본이 새 판으로 넘어간다."""
    compat = load_compat()
    contract = compat.PATCHES[THINKING_TIME]
    pro_only = "1aa1a216f71e1213c2056efb0db4c4de7c2b2c505311e1be98c2b6a2784521dd"
    assert contract["patched"] != pro_only
    assert pro_only in contract["legacy_patched"]
    assert contract["legacy_patches"][pro_only] == "thinkingTime.gpt56-pro-power-slider.patch"


def _stub_openai(root: Path) -> None:
    """errors.js가 openai를 import하는 탓에 패키지 단독으로는 로드되지 않는다."""
    package = root / "node_modules" / "openai"
    package.mkdir(parents=True)
    (package / "package.json").write_text(
        json.dumps(
            {
                "name": "openai",
                "version": "0.0.0-test-stub",
                "type": "module",
                "exports": {".": "./index.js", "./error": "./error.js"},
            }
        ),
        encoding="utf-8",
    )
    (package / "error.js").write_text("export class APIError extends Error {}\n", encoding="utf-8")
    (package / "index.js").write_text(
        "export class APIConnectionError extends Error {}\n"
        "export class APIConnectionTimeoutError extends Error {}\n"
        "export class APIUserAbortError extends Error {}\n"
        "export default class OpenAI {}\n",
        encoding="utf-8",
    )


def test_published_0180_thinking_time_drives_the_effort_power_slider(tmp_path: Path) -> None:
    compat = load_compat()
    configured = os.environ.get("ORACLE_018_PACKAGE_ROOT", "").strip()
    source = Path(configured) if configured else Path("__oracle_018_cache_unset__")
    if not source.is_dir():
        if os.environ.get("CI"):
            pytest.fail("CI must prepare the exact published Oracle 0.18.0 package")
        pytest.skip("published Oracle 0.18.0 package root is unavailable")
    package = tmp_path / "oracle-thinking-effort"
    shutil.copytree(source, package)
    result = compat.ensure_oracle_compatibility(
        "oracle 0.18.0",
        package_root=package,
        backup_root=tmp_path / "backup",
    )
    assert THINKING_TIME in set(result["changed"]) | set(result["already_patched"])
    assert compat.sha256_file(package / THINKING_TIME) == compat.PATCHES[THINKING_TIME]["patched"]

    node = shutil.which("node")
    assert node is not None
    _stub_openai(package)
    module_url = (package / THINKING_TIME).resolve().as_uri()
    # 이 파일이 만드는 것은 브라우저에서 평가될 "문자열"이다. 템플릿 리터럴 안의
    # 이스케이프가 어긋나면 파이썬/노드 어느 쪽도 아프지 않고 실행 시점에만 죽는다
    # (0.17.1의 broken-power/double-escaped-power/regex-power 판들이 그렇게 나왔다).
    # 그래서 산출 문자열 자체를 tier마다 파싱해 본다.
    script = f"""
import vm from "node:vm";
import {{ buildThinkingTimeExpressionForTest }} from {json.dumps(module_url)};
const levels = {json.dumps(list(LEVELS))};
const report = {{}};
for (const level of levels) {{
  const expression = buildThinkingTimeExpressionForTest(level, "gpt-5.6");
  new vm.Script("(" + expression + ")");
  report[level] = {{
    slider: expression.includes("selectEffortPowerSlider"),
    // option-not-found 경로가 피커를 열어둔 채 반환하면 비엄격 호출자의 제출이
    // 그 레이어에 먹혀 "Prompt did not appear in conversation"으로 죽는다.
    closesOnMiss: expression.includes(
      "const result = failure('option-not-found', {{ modelKind: triggerModelKind }});"
    ),
  }};
}}
console.log(JSON.stringify(report));
"""
    script_path = package / "thinking-effort-check.mjs"
    script_path.write_text(script, encoding="utf-8")
    completed = subprocess.run(
        [node, str(script_path)],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(package),
    )
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout.strip().splitlines()[-1])
    assert set(report) == set(LEVELS)
    for level, facts in report.items():
        assert facts["slider"] is True, level
        assert facts["closesOnMiss"] is True, level
