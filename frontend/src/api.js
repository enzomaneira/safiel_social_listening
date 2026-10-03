import axios from 'axios';

export const api = axios.create({ baseURL: import.meta.env.VITE_API_URL || 'http://localhost:5000/api' });
api.interceptors.request.use((config) => {
  const token = sessionStorage.getItem('social-listening-token');
  if (token) config.headers.Authorization = `Bearer ${token}`;
  return config;
});
api.interceptors.response.use((response) => response, (error) => {
  if (error.response?.status === 401) {
    sessionStorage.removeItem('social-listening-token');
    window.dispatchEvent(new Event('session-expired'));
  }
  return Promise.reject(error);
});
