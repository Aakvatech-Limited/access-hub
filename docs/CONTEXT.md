# Project context

## User story

## Problem Statement

I run an ERPNext implementation / consultancy company. I have hundreds of end-user clients who use ERPNext for their business ops. I have tens of employees who work on these implementations and support these clients.

The way we operate right now is that each employee has their user account on client sites. This user needs to be System Manager because many of the tasks they perform require elevated permissions.

Recently, despite having signed NDAs, we have had a few incidents where employees have misused their elevated permissions on client sites. This has led to data breaches and loss of trust with our clients.

More context: I have a central helpdesk site where clients and employees can interact over tickets.

## Proposed Solution

I think there are two parts to what I'm thinking:

1. Ephemeral access

Employee clicks a button on the helpdesk site - gets access (with/without approval - optional). Once his job is done, the User on client's site is deleted automatically. This way, employees only have access when they need it and cannot misuse their permissions outside of that window.

2. Logging

All actions performed by employees on client sites should be logged and monitored. This includes tracking which employee accessed which client site, what actions they performed, and when. This will help in auditing and identifying any misuse of permissions.

Logging cannot happen on the client site database because employees can delete logs. So, the logging should happen in files on the client site. These may be synced periodically to the central helpdesk site for monitoring and auditing purposes.

We do not give SSH access to client site, so logging can be done in a file in the site's dir.

## Questions

- what's a clean software design to provision this ephemeral access? ideally, without having to create a base user on each client site and copy the api key-pair to the helpdesk site.

i will install a separate frappe app on both sites (helpdesk and client).
## FC automation scope

# fc-automation-scope

