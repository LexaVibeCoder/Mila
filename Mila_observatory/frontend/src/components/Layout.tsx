import React from 'react';
import { NavLink } from 'react-router-dom';

const navItems = [
  { path: '/', label: 'Dashboard', icon: '◉' },
  { path: '/pipeline', label: 'Pipeline', icon: '▷' },
  { path: '/users', label: 'Users', icon: '☺' },
  { path: '/model', label: 'Model', icon: '◈' },
  { path: '/sleep', label: 'Sleep', icon: '◍' },
  { path: '/memory', label: 'Memory', icon: '▤' },
  { path: '/config', label: 'Runtime State', icon: '▦' },
  { path: '/tools', label: 'Tools', icon: '⊞' },
  { path: '/personality', label: 'Personality', icon: '◇' },
  { path: '/state-agent', label: 'State Agent', icon: '◔' },
  { path: '/test-chat', label: 'Test Chat', icon: '✎' },
];

const styles: Record<string, React.CSSProperties> = {
  container: {
    display: 'flex',
    minHeight: '100vh',
  },
  sidebar: {
    width: '220px',
    background: 'var(--bg-secondary)',
    borderRight: '1px solid var(--border)',
    padding: '20px 0',
    display: 'flex',
    flexDirection: 'column',
  },
  logo: {
    padding: '20px 20px',
    borderBottom: '1px solid var(--border)',
    marginBottom: '10px',
    display: 'flex',
    flexDirection: 'column',
    alignItems: 'center',
    textAlign: 'center' as const,
    gap: '10px',
  },
  logoImg: {
    width: '64px',
    height: '64px',
    borderRadius: '18px',
    flexShrink: 0,
    boxShadow: '0 0 24px rgba(108, 92, 231, 0.55)',
  },
  logoTitle: {
    color: 'var(--accent)',
    fontSize: '18px',
    fontWeight: 700,
    letterSpacing: '1px',
  },
  logoSub: {
    color: 'var(--text-secondary)',
    fontSize: '11px',
    marginTop: '4px',
  },
  nav: {
    display: 'flex',
    flexDirection: 'column',
    gap: '2px',
    padding: '0 8px',
  },
  link: {
    display: 'flex',
    alignItems: 'center',
    gap: '10px',
    padding: '10px 12px',
    borderRadius: '8px',
    color: 'var(--text-secondary)',
    textDecoration: 'none',
    fontSize: '14px',
    transition: 'all 0.2s',
  },
  linkActive: {
    background: 'var(--bg-hover)',
    color: 'var(--accent)',
  },
  icon: {
    width: '20px',
    textAlign: 'center' as const,
    fontSize: '16px',
  },
  main: {
    flex: 1,
    padding: '24px',
    overflow: 'auto',
  },
};

export default function Layout({ children }: { children: React.ReactNode }) {
  return (
    <div style={styles.container}>
      <aside style={styles.sidebar}>
        <div style={styles.logo}>
          <img src="/icons/observatory-logo.png" alt="Mila Observatory" style={styles.logoImg} />
          <div>
            <div style={styles.logoTitle}>Mila Observatory</div>
            <div style={styles.logoSub}>Consciousness Lab</div>
          </div>
        </div>
        <nav style={styles.nav}>
          {navItems.map(item => (
            <NavLink
              key={item.path}
              to={item.path}
              end={item.path === '/'}
              style={({ isActive }) => ({
                ...styles.link,
                ...(isActive ? styles.linkActive : {}),
              })}
            >
              <span style={styles.icon}>{item.icon}</span>
              {item.label}
            </NavLink>
          ))}
        </nav>
      </aside>
      <main style={styles.main}>
        {children}
      </main>
    </div>
  );
}