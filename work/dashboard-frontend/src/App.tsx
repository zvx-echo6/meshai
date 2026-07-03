import { Routes, Route } from 'react-router-dom'
import Layout from './components/Layout'
import Dashboard from './pages/Dashboard'
import Mesh from './pages/Mesh'
import Environment from './pages/Environment'
import Config from './pages/Config'
import Alerts from './pages/Alerts'
import Notifications from './pages/Notifications'
import Reference from './pages/Reference'
import AdapterConfig from './pages/AdapterConfig'
import GaugeSites from './pages/GaugeSites'
import TownAnchors from './pages/TownAnchors'
import MeshCoreRouting from './pages/MeshCoreRouting'
import MeshCoreConnection from './pages/MeshCoreConnection'
import MeshCoreCompanion from './pages/MeshCoreCompanion'
import MeshtasticConnection from './pages/MeshtasticConnection'
import MeshtasticSources from './pages/MeshtasticSources'
import Places from './pages/Places'
import MeshtasticNodes from './pages/MeshtasticNodes'
import MeshCoreContactsCompanion from './pages/MeshCoreContactsCompanion'
import ScheduledBroadcasts from './pages/ScheduledBroadcasts'
import MeshtasticDangerZones from './pages/MeshtasticDangerZones'
import MeshCoreDangerZones from './pages/MeshCoreDangerZones'
import { ToastProvider } from './components/ToastProvider'
import { DirtyProvider } from './context/DirtyContext'

function App() {
  return (
    <DirtyProvider>
    <ToastProvider>
      <Layout>
        <Routes>
          {/* Core routes */}
          <Route path="/" element={<Dashboard />} />
          <Route path="/environment" element={<Environment />} />
          <Route path="/config" element={<Config />} />
          <Route path="/alerts" element={<Alerts />} />
          <Route path="/notifications" element={<Notifications />} />
          <Route path="/reference" element={<Reference />} />
          <Route path="/adapter-config" element={<AdapterConfig />} />

          {/* New aggregated pages */}
          <Route path="/places" element={<Places />} />

          {/* De-navved routes still work */}
          <Route path="/gauge-sites" element={<GaugeSites />} />
          <Route path="/town-anchors" element={<TownAnchors />} />
          <Route path="/mesh" element={<Mesh />} />

          {/* Meshtastic routes */}
          <Route path="/meshtastic/connection" element={<MeshtasticConnection />} />
          <Route path="/meshtastic/sources" element={<MeshtasticSources />} />
          <Route path="/meshtastic/scheduled" element={<ScheduledBroadcasts family="meshtastic" />} />
          <Route path="/meshtastic/nodes" element={<MeshtasticNodes />} />
          <Route path="/meshtastic/danger-zones" element={<MeshtasticDangerZones />} />

          {/* MeshCore routes */}
          <Route path="/meshcore/connection" element={<MeshCoreConnection />} />
          <Route path="/meshcore/routing" element={<MeshCoreRouting />} />
          <Route path="/meshcore/scheduled" element={<ScheduledBroadcasts family="meshcore" />} />
          <Route path="/meshcore/contacts" element={<MeshCoreContactsCompanion />} />
          <Route path="/meshcore/companion" element={<MeshCoreCompanion />} />
          <Route path="/meshcore/danger-zones" element={<MeshCoreDangerZones />} />
        </Routes>
      </Layout>
    </ToastProvider>
    </DirtyProvider>
  )
}

export default App
