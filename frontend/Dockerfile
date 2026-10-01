# Build stage - npm/node only exist here, inside Docker's build process
FROM node:20-alpine AS build
WORKDIR /app
COPY package.json ./
RUN npm install
COPY . .

ARG VITE_PAYMENT_API_URL=http://localhost:8000
ARG VITE_LEDGER_API_URL=http://localhost:8001
ARG VITE_RECON_API_URL=http://localhost:8003
ARG VITE_CLIENT_API_KEY=demo-key-local-only
ARG VITE_SERVICE_AUTH_SECRET=local-dev-only-secret-DO-NOT-USE-IN-PROD
ENV VITE_PAYMENT_API_URL=$VITE_PAYMENT_API_URL
ENV VITE_LEDGER_API_URL=$VITE_LEDGER_API_URL
ENV VITE_RECON_API_URL=$VITE_RECON_API_URL
ENV VITE_CLIENT_API_KEY=$VITE_CLIENT_API_KEY
ENV VITE_SERVICE_AUTH_SECRET=$VITE_SERVICE_AUTH_SECRET

RUN npm run build

# Serve stage - tiny, just nginx + static files, no Node.js at runtime
FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
