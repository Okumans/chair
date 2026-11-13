#pragma once
#include "platform.h"

constexpr int ESP_MAC_WIFI_STA = 0;

inline int esp_read_mac(uint8_t* mac, int) { return esp_wifi_get_mac(WIFI_IF_STA, mac); }
