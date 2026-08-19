from __future__ import annotations

import re
import sys
from pathlib import Path


SEDAPAL_ROOT = Path(r"D:\Sedapal")
GIS_ROOT = Path(r"D:\SEDAPALGIS")
BACKEND_ROOT = Path(r"D:\BD_LOCAL\api-fastapi")
ALLOWED_TABLES = {"supervision", "planillas"}
SOURCE_SUFFIXES = {".ts", ".tsx", ".js", ".mjs", ".py", ".rs"}
IGNORED_PARTS = {"node_modules", ".next", "target", ".git", "dist", "build"}
TABLE_PATTERN = re.compile(r"\.from\(\s*['\"]([^'\"]+)['\"]")
REMOTE_POOL_PATTERN = re.compile(r"get_supabase_pool\(\s*([^\)]+)\)")


def source_files(root: Path):
    for path in root.rglob("*"):
        if path.is_file() and path.suffix in SOURCE_SUFFIXES and not IGNORED_PARTS.intersection(path.parts):
            yield path


def main() -> int:
    violations: list[str] = []
    for root in (SEDAPAL_ROOT / "apps", SEDAPAL_ROOT / "packages"):
        for path in source_files(root):
            text = path.read_text(encoding="utf-8", errors="ignore")
            for match in TABLE_PATTERN.finditer(text):
                if match.group(1) not in ALLOWED_TABLES:
                    line = text.count("\n", 0, match.start()) + 1
                    violations.append(f"{path}:{line}: Supabase table {match.group(1)}")
            if ".rpc(" in text:
                violations.append(f"{path}: Supabase RPC")
            if ".storage." in text or "storage.from(" in text:
                violations.append(f"{path}: Supabase Storage")

    for path in source_files(GIS_ROOT / "src-tauri"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if re.search(r"\b(sqlx|PgPool|DATABASE_URL|MARTIN_DATABASE_URL|postgresql://)\b", text):
            violations.append(f"{path}: direct PostgreSQL/Martin database access")
        if re.search(r"api/v1/auth/(login|refresh|logout)", text):
            violations.append(f"{path}: FastAPI authentication facade")

    for path in source_files(BACKEND_ROOT / "app"):
        if path.name == "database.py":
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for match in REMOTE_POOL_PATTERN.finditer(text):
            argument = match.group(1).strip().strip("'\"")
            if argument not in ALLOWED_TABLES:
                line = text.count("\n", 0, match.start()) + 1
                violations.append(f"{path}:{line}: remote pool without allowed literal table")

    if violations:
        print("\n".join(sorted(set(violations))))
        return 1
    print("Supabase boundary audit passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
