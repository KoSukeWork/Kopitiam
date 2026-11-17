# Antigravity CLI integration validation

Validated on Windows with agy 1.5.10, Python 3.14, and ACP protocol 1 on 2026-11-17.

## Real end-to-end run

The test used a separate project, `KOPITIAM_HOME`, randomly assigned web ports, and
an isolated `--gemini-dir` passed by the external test harness. Production launches
use agy's default directory. The model was `claude-sonnet-5-5`, thought level `low`.
No existing web instance, terminal conversation, MCP configuration, or credentials
were changed. Test conversations and directories were retained.

| Action | Observed result |
|---|---|
| Create through `/api/acp/sessions` with a first task | The task was stored as a normal user message; the detector woke the managed identity |
| Read and reply through the session MCP | `read` returned that message; `send` returned `E2E_FIRST_OK` to user |
| Run `Write-Output ACP_E2E_COMMAND` | A permission request exposed `run_command`, kind `execute`, and allow/reject once options |
| Approve through `/api/acp/permission` | The command exited 0; the agent sent `E2E_COMMAND_OK` |
| Close and recreate the test web server | The persisted identity resumed and sent `E2E_RESUME_OK` after a new message |
| Handoff | The API returned a quoted PowerShell `Set-Location -LiteralPath` command and `agy --conversation <ID>` |
| Take back and send another message | The same session resumed and sent `E2E_TAKEBACK_OK` |
| Close the test server | No agy process launched with the test directory remained |

The test did not open or drive an interactive terminal. The retained end-to-end
conversation ID is `b1b570db-9d99-44e1-bb3e-649e06dfd579`.

## Protocol findings used by the implementation

- `session/resume` resumes without replaying history. `session/load` replays it.
- Set model and thought level with `session/set_config_option`, using `model` and
  `thought_level`; reapply after resume. Mode uses `session/set_mode`.
- A session MCP named `kopitiam` replaces the global MCP with that same name.
  The global process may start briefly, so terminal identity allocation is lazy.
- MCP metadata supplies root and child conversation IDs. Managed identity stays
  fixed; terminal identity follows the root conversation when available.
- Native reads and writes to the system temporary directory can bypass permission
  requests. Kopitiam policy only governs requests agy actually sends.
- ACP creates no terminal presence lock and does not honor one on resume. The host
  therefore checks the terminal byte lock itself. Users must hand off before
  opening a managed conversation in a terminal.
- Transcript user records wrap input in `USER_REQUEST`; model records use
  `PLANNER_RESPONSE`; native and MCP results use `GENERIC` with no stable call ID.

## Automated and browser coverage

Fake ACP subprocess tests cover request correlation, queued prompts, cancellation,
crash recovery, authentication errors, malformed stdout, permissions, and process
tree cleanup. HTTP tests cover tokens, restore write gates, first-task delivery,
handoff/takeback, and reconnect without prompting. Parser fixtures are handwritten.

Browser checks used Edge with fictional agents, messages, and paths. Dark/light
themes, 390 px mobile width, the creation dialog, permission approval, and activity
rendering were checked with no JavaScript errors or horizontal overflow. Screenshots
are in `docs/images/agy-*.png`.

Permission-card browser assertions additionally check case/underscore-insensitive
command fields, nested arguments, literal HTML-like command text, marked truncation,
expanded parameters when an execute request has no command, and prominent file
paths for file requests. Commands appear above the secondary working-directory path.

## Limits of the validation

- A truly logged-out real agent could not be isolated without changing credentials.
  Authentication error handling is covered by the fake agent, not a real logout.
- Available models and capacity can change. Argon and Flash returned capacity errors
  during probing; the end-to-end run used Sonnet successfully.
- Interactive terminal takeover was not automated. A separate isolated print-mode
  probe confirmed the conversation ID and byte-lock behavior.
