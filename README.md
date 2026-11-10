# feishu-codex-bridge

A local bridge between Feishu group mentions and existing Codex desktop chats. Send a request to a bound chat, keep its project context, and receive the final reply in the original group.

Part of the optional input channels for [codex-superagent-sample](https://github.com/ZhangG0/codex-superagent-sample). It also works without the Workflow service.

## What it does

- Accepts text messages mentioning the configured bot in explicitly bound groups.
- Routes by project alias when a group has multiple target chats.
- Saves incoming jobs, stable task numbers and outgoing replies in SQLite.
- Sends acknowledgements and final replies through Feishu's reply API, with durable retries.
- Uses the desktop `codex queue` transport; keeps the target chat's model, working directory and approval policy.
- Retains uncertain sends for review instead of blindly resubmitting a request.
- Optionally forwards input to the SuperAgent Workflow service and reads its per-team mode from the shared local database.

## Requirements

Python 3.12+ with OpenSSL, a Feishu application with bot and long-connection events enabled, and a Codex desktop installation with a compatible `codex queue` command. The current bridge uses Unix process locking and targets macOS/Linux; native Windows execution has not been validated.

## Install

```sh
git clone https://github.com/ZhangG0/feishu-codex-bridge.git
cd feishu-codex-bridge
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp config.example.json config.json
```

Provide `FEISHU_APP_ID` and `FEISHU_APP_SECRET` through your local environment, or save them in private `var/credentials.json` with `app_id` and `app_secret` fields. Keep these values out of Git and command history.

In Feishu's application console, enable the bot, subscribe to `im.message.receive_v1` using long connections, grant the minimum receive-mention and send-message permissions, publish the app, and add the bot to your group. Set `bot_open_id`, `bindings[].chat_id` and the existing Codex `bindings[].thread_id` in private `config.json`. Use `bridge threads` to inspect available chats. Verify the target before enabling `send_enabled`.

```sh
.venv/bin/python -m bridge threads
.venv/bin/python -m bridge run
```

For a single binding: `@bot your request`. For multiple bindings: `@bot ProjectAlias your request`. Only text messages in bound groups mentioning this bot are accepted. Group members cannot change routing or permissions through messages. Availability to other members also depends on your Feishu tenant's app settings.

Run only one Bridge instance per state directory. The default database is private `var/bridge.sqlite3`; it contains message text. Use SQLite's backup API for a live database so WAL data is retained.

## Use with SuperAgent Workflow

Clone [codex-superagent-sample](https://github.com/ZhangG0/codex-superagent-sample) beside this repository and follow its [Workflow guide](https://github.com/ZhangG0/codex-superagent-sample/tree/main/workflow). In Bridge's private configuration, set `workflow.enabled` to true and provide the local service URL, token path, spool directory and project key. A binding can set `workflow_team_key` when its team key differs from its project key.

Bridge reads the shared Workflow SQLite ledger directly. The default data location follows macOS, Windows and Linux user data conventions; `WORKFLOW_DATA_DIR` or `workflow.mode_database` can align a custom installation.

- **Team workflow on:** incoming messages are safely spooled, acknowledged after durable storage, and forwarded to the Workflow service. Workflow owns task tracking and result delivery.
- **Team workflow off:** Bridge queues new messages directly for the bound Codex chat and replies with the result. Only Bridge and the Codex desktop queue need to remain running; the Workflow service can stop.
- **Unreadable mode database:** input remains buffered for Workflow. A service failure does not imply permission to bypass it.

On a mode change, a spooled message is moved to the direct queue only if it has not already been registered in Workflow. Already registered work remains in the Workflow ledger. Existing direct jobs keep their original route when the switch changes again.

## Recovery and limits

A crashed running job becomes `needs_review`; later jobs for that chat pause. The reviewer checks existing turns without resending. Completed jobs can have their results reconciled; confirmed failed or interrupted turns end without automatic resubmission. Unknown state retains the pause. `bridge status` shows jobs and outbox status; `bridge resolve MESSAGE_ID` is a local manual unblock after reviewing evidence and does not rerun the job.

Outgoing messages use stable IDs and exponential retries, with a maximum of 20 attempts. This reduces duplicates within Feishu's deduplication window; permanent exactly-once delivery is not guaranteed. The first desktop-reply scan establishes a baseline instead of broadcasting old history. One worker processes the direct queue serially.

The bridge does not alter Codex permission settings or bypass desktop approvals. Optional approval cards are described in [remote approvals](docs/remote-approvals.md); they require separate hook and administrator configuration. Images, attachments and full group-history ingestion are not implemented.

## Background operation

An example macOS launchd file is provided at `deploy/com.example.feishu-codex-bridge.example.plist`. Copy it to a private local plist, replace every `/absolute/path/to/feishu-codex-bridge` with your actual checkout, verify executable paths and load that copy. `RunAtLoad` starts it when loaded; `KeepAlive` restarts it after exit. Login startup requires a separate user LaunchAgents installation. Keep your computer online and Codex available.

## Development

```sh
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

Tests use fake Codex/Feishu transports and temporary state. A live Feishu mention and successful original-channel reply are still required to verify a real deployment.

## License

[MIT](LICENSE).
