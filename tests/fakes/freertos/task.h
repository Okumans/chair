#pragma once
#include <cstdint>

namespace FakeTask {
  inline bool defer             = false;
  inline bool fail              = false;
  inline void (*pending)(void*) = nullptr;
  inline void* pendingArgument  = nullptr;

  inline void run() {
    auto fn       = pending;
    auto argument = pendingArgument;
    pending       = nullptr;
    fn(argument);
  }
}  // namespace FakeTask

inline int xTaskCreate(void (*fn)(void*), const char*, uint32_t, void* argument, int, void*) {
  if (FakeTask::fail) return 0;
  if (FakeTask::defer) {
    FakeTask::pending         = fn;
    FakeTask::pendingArgument = argument;
  } else {
    fn(argument);
  }
  return pdPASS;
}

inline void vTaskDelete(void*) {}
