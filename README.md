# UVC Camera on Rockchip 3588(s)

### Requirements
##### UV (recommended)
`sudo apt update && sudo apt install libxcb-cursor0`
1. `curl -LsSf https://astral.sh/uv/install.sh | sh` or `wget -qO- https://astral.sh/uv/install.sh | sh` to install `uv`, skip if `uv` is ready for use.
2. `uv sync` to install the requirements
3. Set the `include-system-site-packages = true` in `pyvenv.cfg` under the `.venv` folder.
##### Setup venv by yourself
1. `sudo apt install python3-venv`
2. `python3 -m venv .venv`
3. Set the `include-system-site-packages = true` in `pyvenv.cfg` under the `.venv` folder.
4. install requirements by yourself

### Single camera test
scripts are under `scirpts` folder

### Dual camera test and capture
`cd` into `src` folder and then run `uv run python main.py`
