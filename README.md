# Plum ecoMAX for Home Assistant

**Plum ecoMAX** is a custom integration for Home Assistant that allows you to monitor and control Plum ecoMAX boiler controllers via a network connection (RS485/Ethernet).

{% if installed %}
## 🚀 Getting Started

Once installed via HACS, you can set up the integration via the Home Assistant generic **Settings** > **Devices & Services** > **Add Integration** menu. Search for "Plum ecoMAX".
{% endif %}

## ✨ Features

This integration aims to support the primary functions of the ecoMAX controller:

* **Monitoring:** Read current temperatures (feeder, boiler, outside, etc.), boiler status, and fuel consumption.
* **Control:** Adjust target temperatures and operation modes.
* **Sensors:** Binary sensors for errors, pumps, and fan status.

## 📦 Installation

### Option 1: HACS (Recommended)
1.  Open HACS in your Home Assistant instance.
2.  Go to "Integrations" and click the three dots in the top right corner.
3.  Select "Custom repositories".
4.  Add the URL of this repository: `https://github.com/lachand/plum_ecomax`.
5.  Select **Integration** as the category.
6.  Click **Add** and then install the integration.
7.  **Restart Home Assistant.**

### Option 2: Manual Installation
1.  Download the `plum_ecomax` directory from the `custom_components` folder in this repository.
2.  Copy the directory into your Home Assistant `<config>/custom_components/` directory.
3.  **Restart Home Assistant.**

## ⚙️ Configuration

Configuration is done via the **User Interface (Config Flow)**.
You will need to provide:
* **IP Address:** The local IP address of your ecoMAX module.
* **Port:** The port used for communication (default usually 8899).

## ⚠️ Disclaimer

This integration is developed by the community and is **not** officially affiliated with or endorsed by Plum Sp. z o.o. Use it at your own risk.

---
*If you encounter any issues, please open a ticket on [GitHub Issues](https://github.com/lachand/plum_ecomax/issues).*
