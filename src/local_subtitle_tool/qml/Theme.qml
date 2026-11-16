pragma Singleton
import QtQuick

// All colors live here. Components must not hard-code colors so that the light and
// dark themes stay consistent. `dark` is bound to bridge.darkMode in Main.qml.
QtObject {
    property bool dark: true

    readonly property color canvas: dark ? "#161615" : "#eeede9"
    readonly property color paper: dark ? "#1e1e1c" : "#fbfbf9"
    readonly property color paperDeep: dark ? "#262624" : "#f5f4f0"
    readonly property color rail: dark ? "#191918" : "#f7f6f3"
    readonly property color hover: dark ? "#2c2b29" : "#efeee9"
    readonly property color selection: dark ? "#3a3936" : "#e4e2dc"

    readonly property color ink: dark ? "#ecebe7" : "#22211f"
    readonly property color inkSoft: dark ? "#b3b1ab" : "#5f5d58"
    readonly property color inkFaint: dark ? "#85837e" : "#8a8882"

    readonly property color line: dark ? "#353432" : "#dad8d2"
    readonly property color lineDark: dark ? "#4a4844" : "#bdbab3"

    readonly property color danger: dark ? "#e38b84" : "#a13e39"
    readonly property color dangerSoft: dark ? "#3a2624" : "#f5eae8"
    readonly property color dangerLine: dark ? "#7a4a46" : "#c9a5a1"

    // Primary action: light button on dark paper; on light paper a pale, low-saturation
    // sage tint with dark text (contrast 12:1) instead of a dark block.
    readonly property color primary: dark ? "#e6e4df" : "#d8e1de"
    readonly property color primaryHover: dark ? "#ffffff" : "#cbd6d2"
    readonly property color primaryBorder: dark ? "#e6e4df" : "#9fb0ab"
    readonly property color onPrimary: dark ? "#1b1b1a" : "#1c2321"

    // Toast notification surface.
    readonly property color notice: dark ? "#e6e4df" : "#22211f"
    readonly property color onNotice: dark ? "#1b1b1a" : "#ffffff"

    readonly property string family: "Malgun Gothic"
}
