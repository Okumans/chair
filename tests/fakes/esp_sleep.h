#pragma once
#include <cstdint>

constexpr int ESP_OK = 0;
enum esp_sleep_wakeup_cause_t {
  ESP_SLEEP_WAKEUP_UNDEFINED,
  ESP_SLEEP_WAKEUP_GPIO,
  ESP_SLEEP_WAKEUP_TIMER
};
enum esp_deepsleep_gpio_wake_up_mode_t { ESP_GPIO_WAKEUP_GPIO_LOW, ESP_GPIO_WAKEUP_GPIO_HIGH };

esp_sleep_wakeup_cause_t esp_sleep_get_wakeup_cause();
int esp_deep_sleep_enable_gpio_wakeup(uint64_t mask, esp_deepsleep_gpio_wake_up_mode_t mode);
int esp_sleep_enable_timer_wakeup(uint64_t us);
void esp_deep_sleep_start();
