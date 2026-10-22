Polymarket Monitor & Auto-Trader

A Python-based toolkit we built for interacting with the Polymarket Central Limit Order Book (CLOB). This project provides automated capabilities for market monitoring, algorithmic trading, and portfolio visualization using the official Polymarket API.

Features

Market Monitoring: Track real-time prices, odds, and order book states on Polymarket.

Automated Trading: Execute programmatic trading strategies and manage orders automatically.

Analytics Dashboard: Visualize market trends, active positions, and portfolio metrics.

Authentication Testing: Verify API credentials and CLOB client connectivity safely before deploying funds.

Cloud Deployment Ready: Includes a Procfile for straightforward deployment to platforms like Heroku.

Repository Structure

polymarket_monitor.py: Core script for market data ingestion and continuous monitoring.

autotrader.py: Algorithmic trading execution and order management.

dashboard.py: Front-end script for visualizing market data and account status.

test_auth.py: Utility script to test and validate your Polymarket API keys.

manual_history.json: Local JSON storage for logging trade history and transaction records.

requirements.txt: Required Python dependencies.

Procfile: Startup configuration for cloud hosting environments.

Prerequisites

Python 3.8+

A Polymarket account with generated API credentials (Host, Key, Secret, and Passphrase) for CLOB access.

Installation

Clone the repository and navigate to the project directory:

cd polymarket-monitor-main


Install the required dependencies:

pip install -r requirements.txt


Quick Start

Set up your environment variables with your Polymarket API credentials.

Test your connection to ensure everything is configured correctly:

python test_auth.py


Once authenticated, you can launch the monitoring script (polymarket_monitor.py), start the auto-trader (autotrader.py), or boot up the visual dashboard (dashboard.py).
