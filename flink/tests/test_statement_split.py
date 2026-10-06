"""Terraform splits each Flink SQL file on ';' (flink_statements.tf): a semicolon inside a comment adds a statement."""
from pathlib import Path

FLINK = Path(__file__).resolve().parents[1]


def test_no_semicolons_in_sql_comments_and_two_statements_per_file():
    for path in sorted(FLINK.glob("*.sql")):
        text = path.read_text()
        commented = [n for n, line in enumerate(text.splitlines(), 1) if "--" in line and ";" in line.split("--", 1)[1]]
        assert not commented, f"{path.name}: ';' inside a comment on lines {commented} splits the statement"
        statements = [s for s in text.split(";") if s.strip() and not all(l.strip().startswith("--") or not l.strip() for l in s.splitlines())]
        assert len(statements) == 2, f"{path.name}: {len(statements)} statements, expected CREATE TABLE + INSERT"
