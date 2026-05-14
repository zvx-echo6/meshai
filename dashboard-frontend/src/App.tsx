import { Routes, Route } from 'react-router-dom'
import Layout from './components/Layout'
import Dashboard from './pages/Dashboard'
import Mesh from './pages/Mesh'
import Environment from './pages/Environment'
import Config from './pages/Config'
import Alerts from './pages/Alerts'
import Notifications from './pages/Notifications'
import Reference from './pages/Reference'
import { ToastProvider } from './components/ToastProvider'

function App() {
  return (
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
        </Routes>
      </Layout>
    </ToastProvider>
  )
}

export default App
