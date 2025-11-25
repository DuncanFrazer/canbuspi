
# Development History

A structured summary of project goals and reasoning.

## Origins
Initially the system used Arduino (UNO + MCP2515) to sniff a VW Golf CAN bus. As message rates increased, attention shifted to a Raspberry Pi for logging, processing and an integrated control UI.

## Major Design Choices
- **Flask**: lightweight, easy to deploy and debug.
- **Python-can**: gives socketcan access on Pi.
- **Manual tagging**: helps correlate CAN data with driver actions.
- **Systemd service**: ensures auto-start on boot.

## Future Work
- Add CSV rotation
- Add frontend plotting
- Add CAN‑Tx replay sandbox
