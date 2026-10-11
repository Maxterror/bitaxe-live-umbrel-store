# Bitaxe Live

**Live monitoring for Bitaxe and HarloOS miners on Umbrel.**

Bitaxe Live is a self-hosted monitoring dashboard designed to help you monitor your Bitaxe miners, track mining performance, customize your dashboard, and integrate mining statistics with Discord.

## Features

### 📊 Live Mining Dashboard
- Monitor hashrate, ASIC and VR temperatures, fan speed, uptime, best share difficulty, share statistics, and pool latency.
- View available miner telemetry and performance information.
- Monitor compatible Bitaxe and HarloOS devices.

### 📈 Live Graphs
- Visualize mining performance over time.
- Track hashrate, power consumption, and temperature data.
- Customize graph colors and dashboard appearance.

### 🎨 Dashboard Customization
- Customize dashboard titles and logos.
- Configure backgrounds and transparency.
- Adjust fonts, text sizes, graph colors, and temperature thresholds.

### 🤖 Discord Integration
- Send mining statistics and dashboard images to Discord channels.
- Access mining information through supported Discord slash commands.
- Start, pause, or restart mining on compatible devices using supported commands and permissions.

### 🔗 Multi-Bot Management
- Configure and manage up to 10 Discord bots.
- Configure each bot independently with its own token, server, channel, and administrator role IDs.
- Enable or disable individual bots and slash-command functionality.

Supported commands include `/dashboard`, `/miners`, `/miner1` through `/miner20`, `/start`, `/pause`, and `/restart`, subject to configuration and permissions.

### 🌡️ Mining Alerts
- Configure temperature thresholds.
- Monitor changes in the blocks-mined counter.

## Installation

1. Install Bitaxe Live through the Umbrel Community App Store.
2. Open the application and complete the initial setup.
3. Add your miner and configure its connection settings.
4. Customize your dashboard and optionally configure Discord integration.

## Requirements

- An Umbrel server.
- A compatible Bitaxe miner or supported HarloOS device.
- A network connection between the dashboard and your miner.
- A Discord bot and the required permissions for Discord integration.

## Data and Privacy

Bitaxe Live stores its application settings and persistent data separately from the application container to support data retention across normal restarts and compatible updates.

Discord bot tokens and other configuration values should be kept private. Never share your bot tokens publicly.

## Project

- **Developer:** Bitaxe Live
- **Version:** 1.1.0
- **Umbrel App Store:** [Bitaxe Live](https://github.com/Maxterror/bitaxe-live-umbrel-store)
- **Source Code:** [GitHub Repository](https://github.com/Maxterror/bitaxe-live-umbrel-store)

## Disclaimer

Device compatibility and available controls depend on the miner, firmware, and supported API functionality.
