---
title: Bembos Scheduler
emoji: 🍔
colorFrom: red
colorTo: yellow
sdk: docker
pinned: false
---

# 🍔 Bembos Scheduler

Plataforma de gestión de horarios para hamburguesería peruana.

## Setup

Este Space requiere la variable de entorno `DATABASE_URL` configurada como **Secret**:

1. En tu Space → **Settings → Repository secrets**
2. Nombre: `DATABASE_URL`
3. Valor: tu connection string de Supabase (ver instrucciones abajo)

## Obtener DATABASE_URL de Supabase

1. Entra a [supabase.com](https://supabase.com) → tu proyecto
2. Ve a **Settings → Database → Connection string → URI**
3. Copia la URI que empieza con `postgresql://...`
4. Pégala como valor del secret `DATABASE_URL` en tu HF Space
