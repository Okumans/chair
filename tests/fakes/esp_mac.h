#pragma once

#include "esp_sleep.h"

using esp_err_t        = int;
constexpr int ESP_FAIL = -1;

enum esp_mac_type_t { ESP_MAC_WIFI_STA };

#define MACSTR       "%02x:%02x:%02x:%02x:%02x:%02x"
#define MAC2STR(mac) (mac)[0], (mac)[1], (mac)[2], (mac)[3], (mac)[4], (mac)[5]

esp_err_t esp_read_mac(uint8_t* mac, esp_mac_type_t type);
