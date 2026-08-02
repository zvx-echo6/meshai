import { Routes, Route, Navigate } from 'react-router-dom'
import Layout from './components/Layout'
import Dashboard from './pages/Dashboard'
import Environment from './pages/Environment'
import Config from './pages/Config'
import ActivityLog from './pages/ActivityLog'
import Notifications from './pages/Notifications'
import Reference from './pages/Reference'
import MeshCoreRouting from './pages/MeshCoreRouting'
import MeshCoreConnection from './pages/MeshCoreConnection'
import MeshtasticConnection from './pages/MeshtasticConnection'
import Places from './pages/Places'
import MeshtasticNodes from './pages/MeshtasticNodes'
import MeshCoreContactsCompanion from './pages/MeshCoreContactsCompanion'
import ScheduledBroadcasts from './pages/ScheduledBroadcasts'
import MeshtasticDangerZones from './pages/MeshtasticDangerZones'
import MeshCoreDangerZones from './pages/MeshCoreDangerZones'
import Coverage from './pages/Coverage'
import { ToastProvider } from './components/ToastProvider'
import { DirtyProvider } from './context/DirtyContext'
import ErrorBoundary from './components/ErrorBoundary'

function App() {
  return (
    <DirtyProvider>
    <ToastProvider>
      <Layout>
        <ErrorBoundary>
        <Routes>
          {/* Core routes */}
          <Route path="/" element={<Dashboard />} />
          <Route path="/environment" element={<Environment />} />
          <Route path="/config" element={<Config />} />
          <Route path="/alerts" element={<ActivityLog />} />
          <Route path="/activity" element={<ActivityLog />} />
          <Route path="/meshtastic/routing" element={<Notifications />} />
          {/* Legacy /notifications -> Meshtastic Routing */}
          <Route path="/notifications" element={<Navigate to="/meshtastic/routing" replace />} />
          <Route path="/reference" element={<Reference />} />

          {/* New aggregated pages */}
          <Route path="/places" element={<Places />} />
          <Route path="/coverage" element={<Coverage />} />
          {/* Custom sources folded into Data Feeds; keep old bookmark working */}
          <Route path="/data-sources" element={<Navigate to="/environment" replace />} />

          {/* Legacy /gauge-sites -> Places (Gauge Sites tab) */}
          <Route path="/gauge-sites" element={<Navigate to="/places" replace />} />
          {/* Legacy /town-anchors -> Places (lands on first tab, no ?tab= deep-link) */}
          <Route path="/town-anchors" element={<Navigate to="/places" replace />} />
          {/* Legacy /mesh -> Meshtastic Nodes & Health (Nodes tab) */}
          <Route path="/mesh" element={<Navigate to="/meshtastic/nodes" replace />} />

          {/* Meshtastic routes */}
          <Route path="/meshtastic/connection" element={<MeshtasticConnection />} />
          {/* Legacy /meshtastic/sources -> Nodes & Health (lands on first tab, no ?tab= deep-link) */}
          <Route path="/meshtastic/sources" element={<Navigate to="/meshtastic/nodes" replace />} />
          <Route path="/meshtastic/scheduled" element={<ScheduledBroadcasts family="meshtastic" />} />
          <Route path="/meshtastic/nodes" element={<MeshtasticNodes />} />
          <Route path="/meshtastic/danger-zones" element={<MeshtasticDangerZones />} />

          {/* MeshCore routes */}
          <Route path="/meshcore/connection" element={<MeshCoreConnection />} />
          <Route path="/meshcore/routing" element={<MeshCoreRouting />} />
          <Route path="/meshcore/scheduled" element={<ScheduledBroadcasts family="meshcore" />} />
          <Route path="/meshcore/contacts" element={<MeshCoreContactsCompanion />} />
          {/* Legacy /meshcore/companion -> Contacts & Companion (lands on first tab, no ?tab= deep-link) */}
          <Route path="/meshcore/companion" element={<Navigate to="/meshcore/contacts" replace />} />
          <Route path="/meshcore/danger-zones" element={<MeshCoreDangerZones />} />
        </Routes>
        </ErrorBoundary>
      </Layout>
    </ToastProvider>
    </DirtyProvider>
  )
}

export default App
