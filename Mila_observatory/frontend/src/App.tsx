import React from 'react';
import { Routes, Route } from 'react-router-dom';
import Layout from './components/Layout';
import Dashboard from './pages/Dashboard';
import ModelControl from './pages/ModelControl';
import MemoryEditor from './pages/MemoryEditor';
import PersonalityEditor from './pages/PersonalityEditor';
import ToolsControl from './pages/ToolsControl';
import PipelineViewer from './pages/PipelineViewer';
import ConfigPage from './pages/ConfigPage';
import SleepControl from './pages/SleepControl';
import StateAgentControl from './pages/StateAgentControl';
import UsersList from './pages/UsersList';
import UserDetail from './pages/UserDetail';
import TestChat from './pages/TestChat';

export default function App() {
  return (
    <Layout>
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/users" element={<UsersList />} />
        <Route path="/users/:user_id" element={<UserDetail />} />
        <Route path="/model" element={<ModelControl />} />
        <Route path="/memory" element={<MemoryEditor />} />
        <Route path="/personality" element={<PersonalityEditor />} />
        <Route path="/tools" element={<ToolsControl />} />
        <Route path="/pipeline" element={<PipelineViewer />} />
        <Route path="/config" element={<ConfigPage />} />
        <Route path="/sleep" element={<SleepControl />} />
        <Route path="/state-agent" element={<StateAgentControl />} />
        <Route path="/test-chat" element={<TestChat />} />
      </Routes>
    </Layout>
  );
}
