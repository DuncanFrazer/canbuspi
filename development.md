
# Development History

A structured summary of project goals and reasoning.

## Origins
Initially the system used Arduino (UNO + MCP2515) to sniff a VW Golf CAN bus. As message rates increased, attention shifted to a Raspberry Pi for logging, processing and an integrated control UI.

## Primary Use Case: Automated Rear Camera Control
The goal is to intelligently control rear camera power based on vehicle state to prevent camera errors while saving power.

### Current Production System (ESP32)
**Deployed hardware:**
- ESP32 with relay controlling camera 12V power
- Simple timer-based cycling: 90s OFF every 30 minutes
- Runs 24/7 to ensure camera availability on first unlock/reverse
- **Works well but:** screen error appears if driving during 90s OFF period

**Why this works:**
- Prevents camera lockup issues through periodic power cycling
- Extremely stable operation

**Limitations:**
- No intelligence about vehicle state (speed, lock status)
- Causes annoying error messages when cycling during use
- Timer-based approach is inefficient

### Development Approach (Raspberry Pi)
**Purpose:** Research and development platform for CAN-based solution

**Advantages:**
- Easy to deploy code changes over WiFi
- Capture and analyze CAN traffic
- Prototype intelligent control logic
- Test message spoofing approaches

**Migration plan:** Once proven on Pi, port final solution back to ESP32 with CAN transceiver

### Improved Camera Control Logic (Target)
**Camera ON conditions:**
- Vehicle unlocked AND speed < 10 mph
- Hysteresis: stays on until speed > 15 mph (prevents flickering)

**Camera OFF conditions:**
- Vehicle locked OR speed > 15 mph

### Camera Error Message Spoofing
**Problem:** Cutting camera power causes ECU error (missing heartbeat/status message)

**Solution:** Impersonate the camera by sending its expected CAN messages when physically powered off

**Investigation needed:**
1. Sniff normal camera startup sequence when car powers on
2. Identify camera's CAN ID and heartbeat message pattern
3. Monitor what happens when camera disconnects (error codes on bus)
4. Implement message injection to satisfy ECU requirements
5. Port proven solution to ESP32 platform

## Major Design Choices
- **Flask**: lightweight, easy to deploy and debug.
- **Python-can**: gives socketcan access on Pi.
- **Manual tagging**: helps correlate CAN data with driver actions.
- **Systemd service**: ensures auto-start on boot.

## References
- **MQB-sniffer**: https://github.com/mrfixpl/MQB-sniffer
  - VW Golf MK7 (MQB platform) CAN message documentation
  - Confirmed specs: 11-bit ID, 500 kbps (ISO 15765-4 CAN)
  - Decoded message IDs and data formats for instrument cluster, gearbox, etc.

## Known CAN Message IDs (MQB Platform)
### Request/Response Pairs
- `0x714` / `0x77E` - Instrument cluster
- `0x7E1` / `0x7E9` - Gearbox (DSG)

### Decoded Messages
**RPM (Instrument Cluster)**
- Request: `714 03 22 22 D1`
- Response: `77E 05 62 22 D1 [HI] [LO]` where `(HI << 8 | LO) / 4 = RPM`

**Gear Position (DSG)**
- Request: `7E1 03 22 38 16`
- Response: `7E9 04 62 38 16 [XX]`
  - `0x00` = none, `0x02` = 1st, `0x0C` = reverse

**Gearbox Mode (DSG)**
- Request: `7E1 03 22 38 15`
- Response: `7E9 04 62 38 15 [XX]`
  - `0x00` = P, `0x01` = R, `0x02` = N, `0x03` = D, `0x04` = S, `0x05` = M

**Ambient Light Sensor**
- Request: `714 03 22 22 4D`
- Response: `77E 04 62 22 4D [XX]` where `XX` = 0-255 brightness

## Future Work
- Add CSV rotation
- Add frontend plotting
- Add CAN‑Tx replay sandbox
- Implement message decoder for known MQB IDs
- Add real-time parsing of RPM, gear position, etc.

### Camera Control Features (Development Roadmap)
**Phase 1: CAN Research (Pi)**
- [ ] Decode vehicle speed from CAN bus
- [ ] Decode lock/unlock status from CAN bus
- [ ] Sniff and identify camera heartbeat/status messages
- [ ] Capture error codes when camera disconnects
- [ ] Document all relevant CAN message IDs and formats

**Phase 2: Pi Prototype**
- [ ] Implement speed-based hysteresis logic (on <10mph, off >15mph)
- [ ] Add relay control for camera power switching (or integrate with existing ESP32)
- [ ] Implement camera message spoofing when powered off
- [ ] Test error-free operation with ECU
- [ ] Verify no screen errors during intelligent cycling

**Phase 3: ESP32 Production Migration**
- [ ] Port CAN monitoring code to ESP32
- [ ] Replace timer-based logic with intelligent state-based control
- [ ] Validate long-term stability (24/7 operation)
- [ ] Deploy as replacement for current ESP32 system
