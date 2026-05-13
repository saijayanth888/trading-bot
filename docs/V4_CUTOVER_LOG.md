
## Cutover at 2026-05-13T12:35:34Z
- freqtrade: stopped (image retained for rollback)
- quanta-core: LIVE_ENGINE_MODE=live (rebuilt + recycled)
- rollback: `docker compose start freqtrade && sed -i 's/LIVE_ENGINE_MODE=live/LIVE_ENGINE_MODE=shadow/' .env && docker compose up -d --no-deps quanta-core`
