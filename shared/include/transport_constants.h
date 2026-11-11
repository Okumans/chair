#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <string_view>

namespace Transport {
  /// Provisioned USB role labels, shared by startup callers and hello diagnostics.
  inline constexpr char SENSOR_ROLE[]  = "sensor";
  inline constexpr char GATEWAY_ROLE[] = "gateway";

  /// Transport bounds and hardware settings. Byte counts exclude framing unless named otherwise.
  inline constexpr uint32_t BAUD_RATE               = 115200;
  inline constexpr uint32_t POLL_DELAY_MS           = 2;
  inline constexpr int8_t UNKNOWN_TX_POWER_QUARTERS = -4;
  inline constexpr double QUARTERS_PER_DBM          = 4.0;
  inline constexpr size_t MAX_PAYLOAD               = 1470;
  inline constexpr size_t MAX_LINE                  = 8192;
  inline constexpr size_t USB_QUEUE_DEPTH           = 8;
  inline constexpr size_t RADIO_QUEUE_DEPTH         = 8;
  inline constexpr size_t TX_QUEUE_DEPTH            = 1;
  inline constexpr size_t USB_POLL_BYTES            = 512;
  inline constexpr uint64_t SEND_TIMEOUT_MS         = 1000;
  inline constexpr int8_t TX_POWER_QUARTER_DBM      = 34;
  inline constexpr size_t MAC_BYTES                 = 6;
  inline constexpr std::array<uint8_t, MAC_BYTES> ZERO_MAC{};
  inline constexpr size_t MAC_TEXT_BYTES        = MAC_BYTES * 3;
  inline constexpr size_t KEY_BYTES             = 16;
  inline constexpr size_t SHA256_BYTES          = 32;
  inline constexpr size_t KEY_FINGERPRINT_BYTES = 8;

  /// Stable USB envelope names; they are separate from chair protocol message types.
  namespace UsbKind {
    inline constexpr std::string_view HELLO     = "hello";
    inline constexpr std::string_view TX        = "tx";
    inline constexpr std::string_view RX        = "rx";
    inline constexpr std::string_view TX_RESULT = "tx_result";
    inline constexpr std::string_view ERROR     = "error";
    inline constexpr std::string_view SLEEP     = "sleep";
    inline constexpr std::string_view TELEMETRY = "telemetry";
    inline constexpr std::string_view COMMAND   = "command";
    inline constexpr std::string_view EVENT_ACK = "event_ack";
  }  // namespace UsbKind

  /// Stable structured diagnostic reasons, never free-form text on the USB stream.
  namespace ErrorCode {
    inline constexpr char INVALID_JSON[]                    = "invalid_json";
    inline constexpr char INVALID_PROTOCOL_COMMAND[]        = "invalid_protocol_command";
    inline constexpr char INVALID_TX[]                      = "invalid_tx";
    inline constexpr char WIFI_START_FAILED[]               = "wifi_start_failed";
    inline constexpr char STATION_MAC_FAILED[]              = "station_mac_failed";
    inline constexpr char CONFIGURE_STATION_MACS_AND_KEYS[] = "configure_station_macs_and_keys";
    inline constexpr char KEY_FINGERPRINT_FAILED[]          = "key_fingerprint_failed";
    inline constexpr char RADIO_INIT_FAILED[]               = "radio_init_failed";
    inline constexpr char RADIO_PEER_FAILED[]               = "radio_peer_failed";
    inline constexpr char RADIO_NOT_READY[]                 = "radio_not_ready";
    inline constexpr char RADIO_BUSY[]                      = "radio_busy";
    inline constexpr char INVALID_PAYLOAD[]                 = "invalid_payload";
    inline constexpr char RADIO_SEND_FAILED[]               = "radio_send_failed";
    inline constexpr char RADIO_SEND_TIMEOUT[]              = "radio_send_timeout";
    inline constexpr char INVALID_RADIO_FRAME[]             = "invalid_radio_frame";
    inline constexpr char SLEEP_SETUP_FAILED[]              = "sleep_setup_failed";
    inline constexpr char RADIO_STOP_FAILED[]               = "radio_stop_failed";
    inline constexpr char STORAGE_UNAVAILABLE_OR_CORRUPT[]  = "storage_unavailable_or_corrupt";
  }  // namespace ErrorCode
}  // namespace Transport
