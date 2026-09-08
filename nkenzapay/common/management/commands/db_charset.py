"""Find text columns that cannot hold the characters this platform writes.

A rupee sign is three bytes and a flag emoji is four. On a latin1 column MySQL
does not truncate them, it refuses the whole row -- "Incorrect string value:
'\\xE2\\x82\\xB9'" -- and the request becomes a 500 at the moment somebody
presses a button, not at deploy time.

The trap is that it is selective. An audit line for a transfer priced in CFA
francs reads "FCFA 100,000" and saves without complaint on any charset; the same
line for one priced in rupees reads "Rs 10,000.00" with the real symbol and
brings the request down. So a deployment can be half broken for months and look
fine, depending on which corridor anybody happened to use.

    python manage.py db_charset

Reports the database, then every table and text column that is not utf8mb4, and
prints the statements that would convert them.
"""
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection

WANTED = "utf8mb4"


class Command(BaseCommand):
    help = "Report tables and columns that are not utf8mb4."

    def handle(self, *args, **options):
        if "mysql" not in connection.vendor:
            self.stdout.write(
                f"The database is {connection.vendor}, which has no per-column "
                "character sets. Nothing to check."
            )
            return

        name = settings.DATABASES["default"]["NAME"]
        with connection.cursor() as cursor:
            self.report_database(cursor, name)
            tables = self.wrong_tables(cursor, name)
            columns = self.wrong_columns(cursor, name)

        if not tables and not columns:
            self.stdout.write("")
            self.stdout.write(self.style.SUCCESS(
                "Every table and text column is utf8mb4. A rupee sign, a flag "
                "emoji and an accented name all store correctly."
            ))
            return

        self.stdout.write("")
        self.stdout.write(self.style.ERROR(
            f"{len(tables)} table(s) and {len(columns)} text column(s) cannot "
            "hold what this platform writes."
        ))
        for table, charset in tables:
            self.stdout.write(f"  table  {table:<40} {charset}")
        for table, column, charset in columns:
            self.stdout.write(f"  column {table}.{column:<32} {charset}")

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("To convert"))
        self.stdout.write(
            f"  ALTER DATABASE `{name}` CHARACTER SET utf8mb4 "
            "COLLATE utf8mb4_unicode_ci;"
        )
        for table in sorted({t for t, _ in tables} | {t for t, _, _ in columns}):
            self.stdout.write(
                f"  ALTER TABLE `{table}` CONVERT TO CHARACTER SET utf8mb4 "
                "COLLATE utf8mb4_unicode_ci;"
            )
        self.stdout.write("")
        self.stdout.write(self.style.WARNING(
            "Take a dump first. CONVERT TO rewrites every row in the table, and "
            "a column already holding bytes written as latin1 is converted as "
            "latin1 -- which is right for text that went in through this same "
            "connection and wrong for anything repaired by hand."
        ))

    def report_database(self, cursor, name):
        cursor.execute(
            "SELECT default_character_set_name, default_collation_name "
            "FROM information_schema.schemata WHERE schema_name = %s",
            [name],
        )
        row = cursor.fetchone()
        self.stdout.write(self.style.MIGRATE_HEADING("Database"))
        self.stdout.write(f"  {name}: {row[0]} / {row[1]}" if row else f"  {name}: unknown")

    def wrong_tables(self, cursor, name):
        # Collation carries the charset as its prefix; information_schema does
        # not expose the charset on tables directly.
        cursor.execute(
            "SELECT table_name, table_collation FROM information_schema.tables "
            "WHERE table_schema = %s AND table_collation IS NOT NULL "
            "AND table_collation NOT LIKE %s ORDER BY table_name",
            [name, f"{WANTED}%"],
        )
        return cursor.fetchall()

    def wrong_columns(self, cursor, name):
        cursor.execute(
            "SELECT table_name, column_name, character_set_name "
            "FROM information_schema.columns "
            "WHERE table_schema = %s AND character_set_name IS NOT NULL "
            "AND character_set_name <> %s ORDER BY table_name, column_name",
            [name, WANTED],
        )
        return cursor.fetchall()
