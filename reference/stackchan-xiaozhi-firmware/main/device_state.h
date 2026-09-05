#ifndef _DEVICE_STATE_H_
#define _DEVICE_STATE_H_

enum DeviceState {
    kDeviceStateUnknown,
    kDeviceStateStarting,
    kDeviceStateWifiConfiguring,
    kDeviceStateIdle,
    kDeviceStateConnecting,
    kDeviceStateListening,
    kDeviceStateSpeaking,
    kDeviceStateAudioTesting,
    kDeviceStateActivating,
    kDeviceStateUpgrading,
    kDeviceStateFatalError
};

#endif // _DEVICE_STATE_H_ 