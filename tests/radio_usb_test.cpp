#include <iostream>
#include <map>

#include "../shared/src/radio_transport.cpp"

FakeSerial Serial;

namespace {
  using Fields = std::map<std::string, glz::raw_json>;

  void drain() {
    for (size_t i = 0; i < 256; ++i) Transport::poll();
    assert(Serial.input.empty() && Transport::usbOutput.empty());
  }

  std::vector<Fields> takeMessages() {
    std::vector<Fields> messages;
    std::string_view remaining = Serial.output;
    while (!remaining.empty()) {
      const auto end = remaining.find('\n');
      assert(end != std::string_view::npos);
      Fields fields;
      assert(!glz::read_json(fields, remaining.substr(0, end)));
      messages.push_back(std::move(fields));
      remaining.remove_prefix(end + 1);
    }
    Serial.output.clear();
    return messages;
  }

  void checkNoParseError() {
    for (const auto& message : takeMessages())
      assert(!message.contains("reason") || message.at("reason").str != "\"invalid_json\"");
  }
}  // namespace

int main(int argc, char**) {
  if (argc > 1) Serial.allocationSucceeds = false;
  Transport::begin(Transport::GATEWAY_ROLE, LinkConfig::GATEWAY_MAC, LinkConfig::SENSOR_MAC);
  if (Serial.allocationSucceeds) {
    FakeHardware::beginResult = false;
    FakeHardware::stopResult  = ESP_ERR_WIFI_NOT_INIT;
    assert(!Transport::resume() && Transport::stopped());
    FakeHardware::stopResult = -1;
    assert(!Transport::resume() && !Transport::stopped());
    FakeHardware::stopResult = ESP_OK;
    assert(Transport::suspend() && Transport::stopped());
    FakeHardware::beginResult = true;
  }
  Transport::resume();
  drain();
  if (!Serial.allocationSucceeds) {
    assert(!Transport::available());
    const auto messages = takeMessages();
    assert(std::any_of(messages.begin(), messages.end(), [](const auto& message) {
      return message.contains("reason") && message.at("reason").str == "\"usb_rx_buffer_failed\"";
    }));
    return 0;
  }
  assert(Transport::available() && Serial.rxCapacity >= Transport::MAX_LINE + 1);
  assert(Transport::suspend() && !Transport::available());
  assert(Transport::suspend());
  FakeHardware::startResult = -1;
  assert(!Transport::resume() && !Transport::available());
  FakeHardware::startResult = ESP_OK;
  FakeHardware::peerResult  = -1;
  assert(!Transport::resume() && !Transport::available());
  FakeHardware::peerResult = ESP_OK;
  assert(Transport::resume() && Transport::available());
  assert(Transport::resume());
  FakeHardware::stopResult = -1;
  assert(!Transport::suspend() && Transport::available());
  FakeHardware::stopResult = ESP_OK;
  FakeRadio::autoComplete  = false;
  const uint8_t byte       = 42;
  const auto stale         = FakeRadio::sentCallback;
  assert(Transport::send(&byte, 1, 99));
  fakeRadioTime = 1001000;
  Transport::poll();
  assert(Transport::needsRestart() && !Transport::busy());
  assert(Transport::suspend() && Transport::resume());
  const auto current = FakeRadio::sentCallback;
  assert(current != stale);
  assert(Transport::send(&byte, 1, 100));
  stale(nullptr, ESP_NOW_SEND_SUCCESS);
  Transport::poll();
  assert(Transport::busy());
  current(nullptr, ESP_NOW_SEND_SUCCESS);
  Transport::poll();
  assert(!Transport::busy());
  FakeRadio::autoComplete = true;
  FakeRadio::sent.clear();
  drain();
  takeMessages();
  SmartChair::Frame availability;
  availability.type = SmartChair::Type::READY;
  std::strcpy(availability.id.node.data(), "chair-01");
  availability.id.boot         = 1;
  availability.id.seq          = 1;
  availability.common.occupied = true;
  const auto readyBytes        = SmartChair::encode(availability);
  const uint8_t sensorMac[]{2, 0, 0, 0, 0, 1};
  const esp_now_recv_info_t sender{sensorMac};
  fakeRadioTime = 2000000;
  FakeRadio::recvCallback(&sender, readyBytes.data(), readyBytes.size());
  fakeRadioTime = 2100000;
  Serial.feed("{\"kind\":\"hello\",\"request_id\":77}\n");
  drain();
  const auto helloMessages = takeMessages();
  assert(std::none_of(helloMessages.begin(), helloMessages.end(), [](const auto& message) {
    return message.contains("kind") && message.at("kind").str == "\"telemetry\"";
  }));
  assert(std::any_of(helloMessages.begin(), helloMessages.end(), [](const auto& message) {
    return message.contains("request_id") && message.at("request_id").str == "77";
  }));
  checkNoParseError();

  // A maximum-length line must fit the underlying driver queue before the loop drains it.
  std::string maximum = "{\"kind\":\"hello\"}";
  maximum.resize(Transport::MAX_LINE, ' ');
  Serial.feed(maximum + '\n');
  assert(Serial.dropped == 0);
  drain();
  checkNoParseError();

  // A real bus reset discards an interrupted command before the new host sends its hello.
  Serial.feed("{\"kind\":\"interrupted");
  Transport::poll();
  Serial.resetBus();
  Serial.feed("{\"kind\":\"hello\"}\n");
  drain();
  checkNoParseError();

  std::string line;
  size_t count = 0;
  while (std::getline(std::cin, line)) {
    Transport::UsbCommand command;
    assert(!glz::read<Transport::UsbReadOptions{}>(command, std::string_view(line)));
    SmartChair::Frame expected;
    assert(command.message && SmartChair::fromJson(*command.message, expected));

    const size_t split = line.size() / 2;
    Serial.feed(std::string_view(line).substr(0, split));
    Transport::poll();
    Serial.connected = false;
    Transport::poll();
    Serial.connected = true;
    Serial.feed(line.substr(split) + '\n');
    drain();
    assert(FakeRadio::sent.size() == ++count);
    SmartChair::Frame sent;
    assert(SmartChair::decode(FakeRadio::sent.back(), sent));
    assert(sent.ack_boot == expected.ack_boot && sent.ack_seq == expected.ack_seq);
    checkNoParseError();
  }
  assert(count > 0 && Serial.dropped == 0);

  // Transient false readiness also preserves a JSON line that was only partly written.
  const std::string longReason(128, 'x');
  Transport::printJson(Transport::ErrorMessage{.reason = longReason});
  Transport::poll();
  assert(!Transport::usbOutput.empty());
  Serial.connected = false;
  Transport::poll();
  Serial.connected = true;
  drain();
  takeMessages();

  Serial.feed("{\"kind\":\n");
  drain();
  const auto errors = takeMessages();
  assert(errors.size() == 1);
  const auto& error = errors.front();
  assert(error.at("reason").str == "\"invalid_json\"");
  assert(error.at("parse_error").str != "\"none\"");
  assert(error.at("line_bytes").str == "8");
  assert(error.contains("byte_offset") && !error.contains("input"));
  std::cout << count << " actual bridge ACKs survived split USB input and transient disconnects\n";
}
