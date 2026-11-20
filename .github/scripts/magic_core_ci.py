#!/usr/bin/env python3
"""Run GODOT 4.7 smoke/regression checks on a ZIP stored in the repository.
Only safe, deterministic path-case repairs are attempted. This is not AI auto-repair.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile, BadZipFile, ZIP_DEFLATED

INPUT = Path("MAGIC_CORE_ATUAL.zip")
OUT = Path("ci-output")
PROJECTS = OUT / "working"
LOGS = OUT / "logs"
ERROR = re.compile(r"(?im)^\s*(?:SCRIPT ERROR|ERROR:|Parse Error|Failed to load|Cannot load|Invalid call)")
REFS = re.compile(r"res://[^\s\"'<>()\r\n]+")
TEXT = {".gd", ".tscn", ".tres", ".gdshader", ".cfg", ".godot"}

def record(name, command, timeout=120):
    try:
        proc = subprocess.run(command, capture_output=True, text=True, errors="replace",
                              timeout=timeout, env=dict(os.environ, XDG_DATA_HOME=str((OUT / "user_data").resolve())))
        output = proc.stdout + "\n" + proc.stderr
        ok = proc.returncode == 0 and not ERROR.search(output)
        status = {"passed": ok, "returncode": proc.returncode, "errors":
                  [line[:500] for line in output.splitlines() if ERROR.search(line)][:25]}
    except subprocess.TimeoutExpired:
        output = "Timed out during " + name
        status = {"passed": False, "timeout": True}
    (LOGS / (name + ".log")).write_text(output, encoding="utf-8")
    return status

def prepare():
    if not INPUT.is_file():
        raise RuntimeError("Envie seu ZIP atualizado ao repositório como MAGIC_CORE_ATUAL.zip (raiz do repositório).")
    if not INPUT.read_bytes()[:4] == b"PK\x03\x04":
        raise RuntimeError("MAGIC_CORE_ATUAL.zip não começa com cabeçalho de arquivo ZIP.")
    try:
        with ZipFile(INPUT) as z:
            infos = z.infolist()
            if len(infos) > 20000 or sum(i.file_size for i in infos) > 1_500_000_000:
                raise RuntimeError("ZIP excede os limites de extração segura.")
            root = PROJECTS.resolve()
            for item in infos:
                name = item.filename
                p = (root / name).resolve()
                if (not p.is_relative_to(root) or name.startswith(("/", "\\")) or "\\" in name
                    or ((item.external_attr >> 16) & 0o170000) == stat.S_IFLNK):
                    raise RuntimeError("Entrada inválida ou insegura no ZIP: " + name)
            corrupted = z.testzip()
            if corrupted is not None:
                raise RuntimeError("Arquivo corrompido no ZIP: " + corrupted)
            z.extractall(PROJECTS)
    except BadZipFile as err:
        raise RuntimeError("O ZIP enviado não é válido: " + str(err)) from err
    cfgs = [p for p in PROJECTS.rglob("project.godot") if ".godot" not in p.parts]
    if len(cfgs) != 1:
        raise RuntimeError("O ZIP deve conter exatamente um project.godot; encontrados: " + str(len(cfgs)))
    return cfgs[0].parent

def fix_case_paths(project):
    paths = {p.relative_to(project).as_posix() for p in project.rglob("*")
             if p.is_file() and ".godot" not in p.relative_to(project).parts}
    lookup = {}
    for path in paths:
        lookup.setdefault(path.casefold(), []).append(path)
    fixes = []
    for p in project.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in TEXT or p.stat().st_size > 2_000_000:
            continue
        try:
            old = p.read_text("utf-8")
        except UnicodeDecodeError:
            continue
        new = old
        for match in REFS.finditer(old):
            ref = match.group()[6:].rstrip(",;]")
            if not ref or ref in paths or any(x in ref for x in ("%", "{", "}", "$", "*")):
                continue
            candidates = lookup.get(ref.casefold(), [])
            if len(candidates) == 1:
                before = "res://" + ref
                after = "res://" + candidates[0]
                new = new.replace(before, after)
                fixes.append({"source":p.relative_to(project).as_posix(), "from":before, "to":after})
        if old != new:
            p.write_text(new, encoding="utf-8")
    return fixes

def run(project, round_name):
    base = ["godot", "--headless", "--path", str(project.resolve())]
    tests = {}
    tests["import"] = record(round_name + "-import", base + ["--import"], 180)
    if not tests["import"]["passed"]:
        return tests
    config = (project / "project.godot").read_text("utf-8", errors="replace")
    if re.search(r"(?m)^\s*run/main_scene\s*=", config):
        tests["startup"] = record(round_name + "-startup", base + ["--quit-after", "180"], 100)
    else:
        tests["startup"] = {"passed": True, "skipped": "No run/main_scene configured"}
    for path in sorted((project / "tests").glob("*_regression.gd")):
        tests["regression:" + path.name] = record(
            round_name + "-" + path.stem, base + ["--script", "res://tests/" + path.name], 110)
    return tests

def all_pass(tests):
    return bool(tests) and all(x.get("passed") for x in tests.values())

def archive(project, passed):
    # Always preserve a copy for diagnosis, but NEVER label a failing ZIP as repaired.
    output = OUT / ("MAGIC_CORE_validado_Godot_4_7.zip" if passed else "MAGIC_CORE_pendente_de_correcao.zip")
    with ZipFile(output, "w", ZIP_DEFLATED, compresslevel=6) as z:
        for path in sorted(project.rglob("*")):
            if path.is_file() and ".godot" not in path.relative_to(project).parts:
                z.write(path, (Path("magic_core") / path.relative_to(project)).as_posix())
    with ZipFile(output) as z:
        if z.testzip() is not None:
            raise RuntimeError("O ZIP de saída falhou na validação.")
    return output

def main():
    LOGS.mkdir(parents=True, exist_ok=True)
    result = {"input": INPUT.name, "engine": None, "round1": {}, "safe_repairs": [],
              "round2": None, "status": "not-tested"}
    try:
        project = prepare()
        version = record("godot-version", ["godot", "--version"], 20)
        result["engine"] = (LOGS / "godot-version.log").read_text("utf-8").strip()
        if not version["passed"]:
            raise RuntimeError("Executável do Godot não disponível.")
        result["round1"] = run(project, "round1")
        passed = all_pass(result["round1"])
        if not passed:
            result["safe_repairs"] = fix_case_paths(project)
            # Retest even if there were no safe repairs; no arbitrary code changes.
            result["round2"] = run(project, "round2")
            passed = all_pass(result["round2"])
        result["status"] = "passed" if passed else "failed"
        result["output_zip"] = archive(project, passed).name
    except Exception as e:
        result["status"] = "failed"
        result["failure"] = str(e)
        print("ERRO:", e, file=sys.stderr)
    (OUT / "results.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), "utf-8")
    summary = ("MAGIC CORE / Godot 4.7.2 — " + result["status"] +
               "\nZIP de entrada: " + result["input"] +
               "\nSaída: " + result.get("output_zip", "não gerada") +
               "\nCorreções automáticas seguras: " + str(len(result["safe_repairs"])) +
               "\nConfira os arquivos results.json e logs para erros restantes.\n" +
               "Aviso: testes headless não validam animação visual ou desempenho em Android.\n")
    (OUT / "summary.txt").write_text(summary, "utf-8")
    print(summary)
    return 0 if result["status"] == "passed" else 1

if __name__ == "__main__":
    sys.exit(main())
