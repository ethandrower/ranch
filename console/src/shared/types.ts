export interface AgentConfig {
  name: string;
  worktree: string;
  description?: string;
}

export interface ProjectConfig {
  name: string;
  path: string;
  label?: string;
  config?: string;
  enabled: boolean;
}

export interface RanchConfig {
  agents: AgentConfig[];
  projects: ProjectConfig[];
  configPath: string;
  projectsPath: string;
  ranchDir: string;
}

export interface RanchApi {
  config: {
    get: () => Promise<RanchConfig>;
  };
  app: {
    version: () => Promise<string>;
  };
}

declare global {
  interface Window {
    ranch: RanchApi;
  }
}
