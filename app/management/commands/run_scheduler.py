import asyncio
from django.core.management.base import BaseCommand
from app.scheduler import start as start_scheduler, stop as stop_scheduler


class Command(BaseCommand):
    help = "Run the background price scraping scheduler"

    def handle(self, *args, **options):
        self.stdout.write("Starting scheduler...")
        asyncio.run(self._run())

    async def _run(self):
        await start_scheduler()
        self.stdout.write(f"Scheduler running. Press Ctrl+C to stop.")
        try:
            while True:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            await stop_scheduler()
            self.stdout.write("Scheduler stopped.")
