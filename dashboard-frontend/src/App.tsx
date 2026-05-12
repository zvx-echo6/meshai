import { Routes, Route } from 'react-router-dom'
import Layout from './components/Layout'
import Dashboard from './pages/Dashboard'
import Mesh from './pages/Mesh'
import Environment from './pages/Environment'
import Config from './pages/Config'
import Alerts from './pages/Alerts'

function App() {
  return (
    <Layout>
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/mesh" element={<Mesh />} />
        <Route path="/environment" element={<Environment />} />
        <Route path="/config" element={<Config />} />
        <Route path="/alerts" element={<Alerts />} />
      </Routes>
    </Layout>
  )
}

export default App
