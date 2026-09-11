pragma Singleton
import QtQuick

QtObject {
    // One instance per QML engine; Main binds appearance to the saved preference.
    property bool dark: false
    property bool reducedMotion: false
    readonly property int motionDuration: reducedMotion ? 0 : 120

    readonly property color workspace: dark ? "#202226" : "#fafbfc"
    readonly property color sidebar: dark ? "#191b1f" : "#f0f2f5"
    readonly property color surface: dark ? "#292c31" : "#ffffff"
    readonly property color inset: dark ? "#24272c" : "#f3f5f8"
    readonly property color hover: dark ? "#343941" : "#e8ecf2"
    readonly property color selection: dark ? "#263e5a" : "#dceafe"
    readonly property color border: dark ? "#424750" : "#d9dee6"
    readonly property color text: dark ? "#f0f2f5" : "#202631"
    readonly property color secondaryText: dark ? "#adb6c4" : "#586476"
    readonly property color disabledText: dark ? "#747e8c" : "#8992a0"
    readonly property color accent: dark ? "#8bbaff" : "#0065d1"
    readonly property color accentText: dark ? "#152235" : "#ffffff"
    readonly property color primary: dark ? "#236ac2" : "#0065d1"
    readonly property color primaryText: "#ffffff"
    readonly property color primaryHover: dark ? "#2874d3" : "#005bbd"
    readonly property color primaryPressed: dark ? "#1c5baa" : "#004fa5"
    readonly property color success: dark ? "#83d4a5" : "#227447"
    readonly property color warning: dark ? "#ebbe79" : "#895b13"
    readonly property color danger: dark ? "#ffa49b" : "#b53229"
    readonly property color warningSurface: dark ? "#3a3022" : "#fff4e0"
    readonly property color dangerSurface: dark ? "#3b292b" : "#fff0ed"
    readonly property color shadow: dark ? "#50000000" : "#200e1b30"
    readonly property color scrim: dark ? "#70000000" : "#28121c2c"

    readonly property int spaceXs: 4
    readonly property int spaceSm: 8
    readonly property int spaceMd: 12
    readonly property int spaceLg: 16
    readonly property int spaceXl: 24
    readonly property int controlHeight: 32
    readonly property int iconSize: 16
    readonly property int controlRadius: 8
    readonly property int cardRadius: 12
    readonly property int dialogRadius: 16
    readonly property int captionSmall: 11
    readonly property int caption: 12
    readonly property int body: 13
    readonly property int sectionTitle: 17
    readonly property int pageTitle: 24
}
