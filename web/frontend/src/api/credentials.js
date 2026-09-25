import { deleteRequest, getJson, postJson } from "./client.js";
import { uploadFormData } from "./upload.js";

export const createTextCredential = ({ kind, value }) => postJson("/api/credentials", { kind, value });
export const inspectCredential = (id) => getJson(`/api/credentials/${encodeURIComponent(id)}`);
export const deleteCredential = (id) => deleteRequest(`/api/credentials/${encodeURIComponent(id)}`);
export const uploadCaCertificate = (file) => {
  const form = new FormData(); form.append("file", file, file.name);
  return uploadFormData("/api/certificates/ca", form);
};
export const uploadClientIdentity = ({ certificate, privateKey, privateKeyPassword }) => {
  const form = new FormData();
  form.append("certificate", certificate, certificate.name);
  form.append("private_key", privateKey, privateKey.name);
  if (privateKeyPassword) form.append("private_key_password", privateKeyPassword);
  return uploadFormData("/api/certificates/client-identity", form);
};
