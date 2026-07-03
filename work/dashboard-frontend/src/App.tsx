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
import MeshCoreContacts from './pages/MeshCoreContacts'
import MeshCoreCompanion from './pages/MeshCoreCompanion'
import MeshtasticConnection from './pages/MeshtasticConnection'
import MeshtasticSources from './pages/MeshtasticSources'
import { ToastProvider } from './components/ToastProvider'
import { DirtyProvider } from './context/DirtyContext'

function App() {
  return (
    <DirtyProvider>
    <ToastProvider>
      <Layout>
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/mesh" element={<Mesh />} />
          <Route path="/environment" element={<Environment />} />
          <Route path="/config" element={<Config />} />
          <Route path="/alerts" element={<Alerts />} />
          <Route path="/notifications" element={<Notifications />} />
          <Route path="/reference" element={<Reference />} />
          <Route path="/adapter-config" element={<AdapterConfig />} />
          <Route path="/gauge-sites" element={<GaugeSites />} />
          <Route path="/town-anchors" element={<TownAnchors />} />
          <Route path="/meshcore/routing" element={<MeshCoreRouting />} />
          <Route path="/meshcore/connection" element={<MeshCoreConnection />} />
          <Route path="/meshcore/contacts" element={<MeshCoreContacts />} />
          <Route path="/meshcore/companion" element={<MeshCoreCompanion />} />
          <Route path="/meshtastic/connection" element={<MeshtasticConnection />} />
          <Route path="/meshtastic/sources" element={<MeshtasticSources />} />
        </Routes>
      </Layout>
    </ToastProvider>
    </DirtyProvider>
  )
}

export default App
