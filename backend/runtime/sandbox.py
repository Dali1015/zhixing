# -*- coding: utf-8 -*-
"""
Muse Agent Runtime — 受控 Python 代码解释器沙箱 (Code Interpreter)
能力：
1. 允许 Agent 编写任意数据处理、统计分析、Excel/CSV/Word 解析、图表(SVG/HTML)生成与 .ics 日历构建脚本；
2. AST 静态安全检查拦截危险系统破坏操作；
3. 子进程工作目录直接挂载至 workspace/files/，剥离环境变量密钥，严格执行超时保护。
"""
import ast
import os
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional


BANNED_IMPORTS = {
    "pty", "ctypes", "webbrowser", "antigravity", "tkinter", "turtle",
}
BANNED_ATTR_CALLS = {
    "system", "popen", "spawnl", "spawnle", "spawnlp", "spawnv", "spawnve",
    "execl", "execle", "execlp", "execv", "execve", "execvp", "fork", "kill", "rmtree",
}


def validate_python_safety(code: str) -> Optional[str]:
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return f"Python 语法错误 (行 {exc.lineno}): {exc.msg}"

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root_pkg = alias.name.split(".")[0]
                if root_pkg in BANNED_IMPORTS:
                    return f"安全策略拦截：禁止导入模块 `{root_pkg}`"
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                root_pkg = node.module.split(".")[0]
                if root_pkg in BANNED_IMPORTS:
                    return f"安全策略拦截：禁止从模块 `{root_pkg}` 导入"
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute) and node.func.attr in BANNED_ATTR_CALLS:
                return f"安全策略拦截：禁止调用危险系统方法 `.{node.func.attr}()`"
            if isinstance(node.func, ast.Name) and node.func.id in ("eval", "exec"):
                return f"安全策略拦截：禁止动态调用 `{node.func.id}()`"
    return None


def execute_python_code(
    code: str,
    files_dir: str,
    artifacts_dir: str,
    timeout: int = 12,
) -> Dict[str, Any]:
    err_msg = validate_python_safety(code)
    if err_msg:
        return {
            "ok": False,
            "exit_code": -1,
            "stdout": "",
            "stderr": err_msg,
            "duration_ms": 0,
            "generated_files": [],
        }

    os.makedirs(files_dir, exist_ok=True)
    os.makedirs(artifacts_dir, exist_ok=True)

    before_snap = {}
    for folder in (files_dir, artifacts_dir):
        for fn in os.listdir(folder):
            fp = os.path.join(folder, fn)
            if os.path.isfile(fp):
                before_snap[fp] = os.path.getmtime(fp)

    run_dir = tempfile.mkdtemp(prefix="muse_py_")
    script_path = os.path.join(run_dir, "_agent_task.py")

    # 注入便捷路径常量与纯标准库 Office/文本读取辅助函数供 Agent 脚本直接调用
    prelude = (
        "# -*- coding: utf-8 -*-\n"
        "import os as _os, zipfile as _zf, xml.etree.ElementTree as _ET\n"
        f"FILES_DIR = {repr(files_dir)}\n"
        f"ARTIFACTS_DIR = {repr(artifacts_dir)}\n"
        "def load_xlsx_rows(filename):\n"
        "    p = filename if _os.path.isabs(filename) else _os.path.join(FILES_DIR, filename)\n"
        "    rows_out = []\n"
        "    with _zf.ZipFile(p) as z:\n"
        "        shared = []\n"
        "        if 'xl/sharedStrings.xml' in z.namelist():\n"
        "            for si in _ET.fromstring(z.read('xl/sharedStrings.xml')):\n"
        "                shared.append(''.join(t.text for t in si.iter() if (t.tag.endswith('}t') or t.tag == 't') and t.text))\n"
        "        for sname in sorted(n for n in z.namelist() if n.startswith('xl/worksheets/sheet') and n.endswith('.xml')):\n"
        "            for r in _ET.fromstring(z.read(sname)).iter():\n"
        "                if not (r.tag.endswith('}row') or r.tag == 'row'): continue\n"
        "                vals = []\n"
        "                for c in list(r):\n"
        "                    if not (c.tag.endswith('}c') or c.tag == 'c'): continue\n"
        "                    ct = c.attrib.get('t', '')\n"
        "                    v = ''\n"
        "                    if ct == 'inlineStr':\n"
        "                        v = ''.join(t.text for t in c.iter() if (t.tag.endswith('}t') or t.tag == 't') and t.text)\n"
        "                    else:\n"
        "                        vn = next((x for x in list(c) if x.tag.endswith('}v') or x.tag == 'v'), None)\n"
        "                        if vn is not None and vn.text is not None:\n"
        "                            v = shared[int(vn.text)] if (ct == 's' and vn.text.isdigit() and int(vn.text) < len(shared)) else vn.text\n"
        "                    vals.append(v.strip())\n"
        "                if any(vals): rows_out.append(vals)\n"
        "    return rows_out\n"
        "def load_docx_text(filename):\n"
        "    p = filename if _os.path.isabs(filename) else _os.path.join(FILES_DIR, filename)\n"
        "    W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'\n"
        "    with _zf.ZipFile(p) as z:\n"
        "        root = _ET.fromstring(z.read('word/document.xml'))\n"
        "    return '\\n'.join(''.join(t.text for t in el.iter(W + 't') if t.text) for el in root.iter(W + 'p')).strip()\n"
    )
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(prelude + "\n" + code)

    clean_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "PYTHONIOENCODING": "utf-8",
        "FILES_DIR": files_dir,
        "ARTIFACTS_DIR": artifacts_dir,
        "HOME": run_dir,
    }

    t0 = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, script_path],
            cwd=files_dir,
            env=clean_env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        ms = int((time.time() - t0) * 1000)
        generated: List[str] = []
        for folder in (files_dir, artifacts_dir):
            for fn in sorted(os.listdir(folder)):
                fp = os.path.join(folder, fn)
                if os.path.isfile(fp):
                    mtime = os.path.getmtime(fp)
                    if fp not in before_snap or mtime > before_snap[fp]:
                        generated.append(fn)

        return {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout": (proc.stdout or "")[:10_000],
            "stderr": (proc.stderr or "")[:4_000],
            "duration_ms": ms,
            "generated_files": generated,
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "exit_code": -9,
            "stdout": "",
            "stderr": f"代码执行超时（超过 {timeout} 秒上限）",
            "duration_ms": timeout * 1000,
            "generated_files": [],
        }
    except Exception as exc:
        return {
            "ok": False,
            "exit_code": -2,
            "stdout": "",
            "stderr": str(exc)[:500],
            "duration_ms": int((time.time() - t0) * 1000),
            "generated_files": [],
        }
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
