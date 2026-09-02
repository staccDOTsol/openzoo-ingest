import QtQuick
import Quickshell
import Quickshell.Io

// openzoo ingest — the Omarchy plugin shape of a standalone service.
//
// This QML draws nothing. Its one job is to make sure the two user units that
// do the work exist and are enabled: openzoo-lecore.service (the loopback
// memory daemon) and openzoo-ingest.timer (a bind every ten minutes). It runs
// install.sh from THIS checkout in plugin mode, which links the checkout into
// place and enables the units; a second run is a no-op. Removing the plugin
// leaves the units in place until `openzoo-ingest uninstall` — a memory is
// not something to delete on a shell restart.
//
// Nothing here touches the network. install.sh in plugin mode clones the
// leCore engine once, pinned to an exact commit, and everything after that is
// loopback.
Item {
  id: root
  readonly property string pluginDir: String(Qt.resolvedUrl(".")).replace(/^file:\/\//, "").replace(/\/$/, "")

  Process {
    id: setup
    running: false
    command: []
    stdout: StdioCollector { id: setupOut; waitForEnd: true }
    onExited: function (code) {
      if (code !== 0) console.warn("openzoo-ingest: install.sh --plugin exited " + code + ": " + String(setupOut.text).slice(-400))
    }
  }

  Component.onCompleted: {
    setup.command = ["bash", root.pluginDir + "/install.sh", "--plugin", root.pluginDir]
    setup.running = true
  }
}
