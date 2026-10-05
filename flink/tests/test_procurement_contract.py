"""Offline contract guards, not a Kafka/Avro decoder or a Flink integration test.

Run: python3 -m unittest discover -s overlay/flink/tests -v
The live offset-282 payload is unavailable: do not mistake these checks for a replay.
"""
import json
from pathlib import Path
import re
import unittest


OVERLAY = Path(__file__).resolve().parents[2]


class ProcurementContractTests(unittest.TestCase):
    def test_source_requires_full_before_images_for_flink_retractions(self):
        sql = (OVERLAY / "contracts/procurement/001_procurement.sql").read_text()
        # Ignore comments so merely documenting the requirement cannot pass the guard.
        sql = re.sub(r"--[^\n]*", "", sql)
        self.assertRegex(
            sql,
            r"(?i)ALTER\s+TABLE\s+(?:public\.)?purchase_order\s+REPLICA\s+IDENTITY\s+FULL\s*;",
            "Flink retract input needs FULL before-images on purchase_order updates; "
            "DEFAULT replica identity can produce null/key-only before values",
        )

    def test_connector_preserves_the_debezium_envelope_and_fails_loudly(self):
        config = json.loads((OVERLAY / "connect/connector-procurement-orders.json").read_text())
        self.assertEqual(config["table.include.list"], "public.purchase_order")
        self.assertEqual(config["transforms"], "route")
        self.assertEqual(config["transforms.route.type"],
                         "org.apache.kafka.connect.transforms.RegexRouter")
        self.assertEqual(config["transforms.route.replacement"], "procurement.orders")
        self.assertEqual(config["value.converter"], "io.confluent.connect.avro.AvroConverter")
        self.assertEqual(config.get("errors.tolerance", "none"), "none")

    def test_procurement_sql_keeps_terraform_two_statement_contract(self):
        sql = (OVERLAY / "flink/procurement.sql").read_text()
        # Match flink_statements.tf: split first, THEN strip full-line comments.
        statements = []
        for part in sql.split(";"):
            statement = "\n".join(
                line for line in part.splitlines() if not line.strip().startswith("--")
            ).strip()
            if statement:
                statements.append(statement)
        self.assertEqual(len(statements), 2, "Semicolons in comments also split Terraform statements")
        self.assertTrue(statements[0].startswith("CREATE TABLE IF NOT EXISTS `restock.forecast`"))
        self.assertTrue(statements[1].startswith("INSERT INTO `restock.forecast`"))
        self.assertIn("FROM `procurement.orders`", statements[1])
        self.assertIn("'changelog.mode' = 'upsert'", statements[0])


if __name__ == "__main__":
    unittest.main()
