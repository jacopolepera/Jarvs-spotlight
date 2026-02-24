# Jarvs-spotlight

Jarvs-spotlight is a local AI agent workspace and tool execution environment. 

## Features
- Modular tool execution architecture.
- Workspace environment for managing isolated files.
- Support for complex agent configurations and prompts.
- Built-in logging and debugging tools.

## Setup

1. Clone the repository to your local machine.
2. Install the required dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Set up your `.env` file with the necessary API keys and configuration values. You can use the provided `.env.example` (if applicable) as a template.

## Usage

Refer to the included `settings.json` and `system_prompt.txt` to configure the agent's behavior. The `support/` directory contains helper files, and the `tests/` directory includes the test suite.

## Structure
- `tools/`: Contains the executable tools for the agent.
- `tests/`: Automated test suite for the tools and core logic.
- `workspace/`: The default directory for the agent to write and manage files (ignored by git).
- `logs/`: Execution logs (ignored by git).
