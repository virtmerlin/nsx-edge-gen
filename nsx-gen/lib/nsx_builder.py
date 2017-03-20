#!/usr/bin/env python

# nsx-edge-gen
#
# Copyright (c) 2015-Present Pivotal Software, Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

__author__ = 'Sabha Parameswaran'

import __builtin__
import os.path
import errno
import shutil
import sys
import yaml
import re
import template
import ipcalc
import client
import glob
import mobclient
import subprocess
import xmltodict
from pprint import pprint
from util import *
from nsx_urls import *

LIB_PATH = os.path.dirname(os.path.realpath(__file__))
REPO_PATH = os.path.realpath(os.path.join(LIB_PATH, '..'))

sys.path.append(os.path.join(LIB_PATH, os.path.join('../..')))

import argparse
from print_util  import *
import pynsxv.library.nsx_logical_switch as lswitch
import pynsxv.library.nsx_dlr as dlr
import pynsxv.library.nsx_esg as esg
import pynsxv.library.nsx_dhcp as dhcp
import pynsxv.library.nsx_lb as lb
import pynsxv.library.nsx_dfw as dfw
import pynsxv.library.nsx_usage as usage

DEBUG = False

def initMoidMap(context):
	if 'vmware_mob_moid_map' not in context:
		mobclient.set_context(context['vcenter'])
		moidMap = mobclient.lookup_vsphere_config()
		context['vmware_mob_moid_map'] = moidMap

	mapVcenterResources(context, moidMap)
	return context['vmware_mob_moid_map']

def refreshMoidMap(context):
	if 'vmware_mob_moid_map' not in context:
		return initMoidMap(context)
	else:
		context['vmware_mob_moid_map'].update(mobclient.refresh_vsphere_config())
		return context['vmware_mob_moid_map']
		
def  mapVcenterResources(context, moidMap):
	vcenter_context = context['vcenter']
	vcenter_context['datacenter_id'] = moidMap[vcenter_context['datacenter']]['moid']

	datastore_id = moidMap[vcenter_context['datastore']]['moid']
	if datastore_id is None:
		# Try adding vsan to datastore name
		datastore_id = moidMap['vsan' + vcenter_context['datastore']]['moid']
	vcenter_context['datastore_id'] = datastore_id 
	if DEBUG:
		print 'Vcenter updated context\n'
		pprint(vars(vcenter_context))

def build(context, verbose=False):

	client.set_context(context, 'nsxmanager')
	if DEBUG:
		pprint(refreshMoidMap(context))

	build_logical_switches('lswitch', context, 'logical_switches')
	build_nsx_edge_gateways('edge', context)

def delete(context, verbose=False):
	client.set_context(context, 'nsxmanager')
	refreshMoidMap(context)

	delete_nsx_edge_gateways(context)    
	delete_logical_switches(context, 'logical_switches')

def list(context, verbose=False):
	client.set_context(context, 'nsxmanager')
	if DEBUG:
		pprint(refreshMoidMap(context))    
	
	list_logical_switches(context)
	list_nsx_edge_gateways(context)    
	

def setup_parsers():
	parser = argparse.ArgumentParser(description='PyNSXv Command Line Client for NSX for vSphere')
	parser.add_argument("-i",
						"--ini",
						help="nsx configuration file",
						default="nsx.ini")
	parser.add_argument("-v",
						"--verbose",
						help="increase output verbosity",
						action="store_true")
	parser.add_argument("-d",
						"--debug",
						help="print low level debug of http transactions",
						action="store_true")

	subparsers = parser.add_subparsers()
	lswitch.contruct_parser(subparsers)
	dlr.contruct_parser(subparsers)
	esg.contruct_parser(subparsers)
	dhcp.contruct_parser(subparsers)
	lb.contruct_parser(subparsers)
	dfw.contruct_parser(subparsers)
	usage.contruct_parser(subparsers)

def map_logical_switches_id(logical_switches):
	existinglSwitchesResponse = client.get(NSX_URLS['lswitch']['all'] + '?&startindex=0&pagesize=100')
	existinglSwitchesResponseDoc = xmltodict.parse(existinglSwitchesResponse.text)
	if DEBUG:
		print 'LogicalSwitches response :{}\n'.format(existinglSwitchesResponse.text) 
	
	num_lswitches = len(logical_switches)
	matched_lswitches = 0

	virtualWires = existinglSwitchesResponseDoc['virtualWires']['dataPage']['virtualWire']
	lswitchEntries = virtualWires
	if isinstance(virtualWires, dict):
		lswitchEntries = [ virtualWires ]

	for existingLSwitch in lswitchEntries:
		if (num_lswitches == matched_lswitches):
			break

		for interested_lswitch in  logical_switches: 
			if existingLSwitch['name'] == interested_lswitch['name']:
				interested_lswitch['id'] = existingLSwitch['objectId']

				++matched_lswitches
				break

	print_logical_switches_available(logical_switches)
	for interested_lswitch in  logical_switches: 
		if (interested_lswitch.get('id') is None):
			print 'Logical Switch instance with name: {}'  \
				+ ' does not exist, possibly deleted already'.format(interested_lswitch['name'])
		

def check_logical_switch_exists(vcenterMobMap, lswitchName):

	fullMobName = mobclient.lookupLogicalSwitchManagedObjName(lswitchName)
	# For a lswitch with name ' lswitch-nsx-pcf-CF-Tiles'
	# it would have a MOB name of 'vxw-dvs-29-virtualwire-225-sid-5066-lswitch-nsx-pcf-CF-Tiles'
	if fullMobName is None:
		return False

	if 'vxw-' in fullMobName and '-virtualwire-' in fullMobName:
		print 'Logical switch : {} exists with MOB:{}'.format(lswitchName, fullMobName)
		return True

	return False

def build_logical_switches(dir, context, type='logical_switches', alternate_template=None):

	logical_switches_dir = os.path.realpath(os.path.join(dir ))
	
	if os.path.isdir(logical_switches_dir):
		shutil.rmtree(logical_switches_dir)
	mkdir_p(logical_switches_dir)

	template_dir = '.'
	if alternate_template is not None:
		template_dir = os.path.join(template_dir, alternate_template)

	vdnScopesResponse = client.get(NSX_URLS['scope']['all'])
	vdnScopesDoc = xmltodict.parse(vdnScopesResponse.text)

	if DEBUG:
		print 'VDN Scopes output: {}'.format(vdnScopesDoc)

	vcenterMobMap = refreshMoidMap(context)
	defaultVdnScopeId = vdnScopesDoc['vdnScopes']['vdnScope']['objectId']

	for lswitch in  context[type]: 

		if check_logical_switch_exists(vcenterMobMap, lswitch['name']):
			print 'Skipping creation of Logical Switch:{}'.format(lswitch['name'])			
			continue

		logical_switches_context = {
			'context': context,
			'logical_switch': lswitch,
			#'managed_service_release_jobs': context['managed_service_release_jobs'],
			'files': []
		}    

		template.render(
			os.path.join(logical_switches_dir, lswitch['name'] + '_payload.xml'),
			os.path.join(template_dir, 'logical_switch_config_post_payload.xml' ),
			logical_switches_context
		)

		# Get the vdn scopes
		#  https://10.193.99.20//api/2.0/vdn/scopes
		# After determining the vdn scope, then post to that scope endpoint

		# POST /api/2.0/vdn/scopes/vdnscope-1/virtualwires
		post_response = client.post_xml(NSX_URLS['scope']['all'] + '/' 
					+ defaultVdnScopeId+'/virtualwires', 
				os.path.join(logical_switches_dir, lswitch['name'] + '_payload.xml'))
		data = post_response.text
		print 'Created Logical Switch : {}\n'.format(lswitch['name'])
		if DEBUG:
			print 'Logical switch creation response:{}\n'.format(data)

def list_logical_switches(context, reportAll=True):

	existinglSwitchesResponse = client.get(NSX_URLS['lswitch']['all']+ '?&startindex=0&pagesize=100')#'/api/2.0/vdn/virtualwires')
	existinglSwitchesResponseDoc = xmltodict.parse(existinglSwitchesResponse.text)
	if DEBUG:
		print 'LogicalSwitches response :{}\n'.format(existinglSwitchesResponse.text) 
	
	virtualWires = existinglSwitchesResponseDoc['virtualWires']['dataPage']['virtualWire']
	lswitchEntries = virtualWires
	if isinstance(virtualWires, dict):
		lswitchEntries = [ virtualWires ]
	
	vcenterMobMap = refreshMoidMap(context)
	print_moid_map(vcenterMobMap)
	
	for lswitch in lswitchEntries:
		lswitch['id'] = lswitch['objectId']

		lswitch['moName'] = mobclient.lookupLogicalSwitchManagedObjName(lswitch['name']) 
		if not lswitch.get('moName'):
			lswitch['moName'] = ''

	managed_lswitch_names = [ lswitch['name'] for lswitch in context['logical_switches']]

	if reportAll:
		print_logical_switches_available(lswitchEntries)
	else:
		managedLSwitches = [ ]		
		for lswitch in lswitchEntries:
			if lswitch['name'] in managed_lswitch_names:
				managedLSwitches.append(lswitch)
		
		print_logical_switches_available(managedLSwitches)


def delete_logical_switches(context, type = 'logical_switches'):

	lswitches = context[type]
	map_logical_switches_id(lswitches)
	
	for lswitch in lswitches:
		
		if  lswitch.get('id') is None:
			continue

		retry = True
		while (retry):
			
			retry = False
			delete_response = client.delete(NSX_URLS['lswitch']['all'] + '/' +
					 lswitch['id'])
			data = delete_response.text

			if DEBUG:
				print 'NSX Logical Switch Deletion response:{}\n'.format(data)

			if delete_response.status_code < 400: 
				print 'Deleted NSX Logical Switch:{}\n'.format(lswitch['name'])

			else:
				print 'Deletion of NSX Logical Switch failed, details: {}\n'.format(data)
				if 'resource is still in use' in str(data):
					retry = True 
					print 'Going to retry deletion again... for Logical Switch:{}\n'.format(lswitch['name'])


def build_nsx_edge_gateways(dir, context, alternate_template=None):
	nsx_edges_dir = os.path.realpath(os.path.join(dir ))
	
	if os.path.isdir(nsx_edges_dir):
		shutil.rmtree(nsx_edges_dir)
	mkdir_p(nsx_edges_dir)

	template_dir = '.'
	if alternate_template is not None:
		template_dir = os.path.join(template_dir, alternate_template)


	logical_switches = context['logical_switches']
	
	
	map_logical_switches_id(logical_switches)
	if DEBUG:
		print 'Logical Switches:{}\n'.format(str(logical_switches))

	uplink_switches = [ ]
	
	map_logical_switches_id(uplink_switches)

	empty_logical_switches = xrange(len(logical_switches) + 1, 10) 
	vcenterMobMap = refreshMoidMap(context)
	vm_network_moid = mobclient.lookupMoid('VM Network')

	# Go with the VM Network for default uplink
	nsxmanager = context['nsxmanager']

	uplink_port_switch = nsxmanager['uplink_details'].get('uplink_port_switch')
	if uplink_port_switch is None:
		uplink_port_switch = 'VM Network'
		nsxmanager['uplink_details']['uplink_port_switch'] = uplink_port_switch
		
	# if use_port_switch is set to 'VM Network' or port switch id could not be retreived.
	portSwitchId = mobclient.lookupMoid(uplink_port_switch) 
	if (portSwitchId is None):
		nsxmanager['uplink_details']['uplink_id'] = vm_network_moid
	else:
		nsxmanager['uplink_details']['uplink_id'] = portSwitchId

	
	for nsx_edge in  context['edge_service_gateways']:
	
		opsmgr_routed_component = nsx_edge['routed_components'][0]
		ert_routed_component    = nsx_edge['routed_components'][1]
		diego_routed_component  = nsx_edge['routed_components'][2]
		tcp_routed_component    = nsx_edge['routed_components'][3]


		for routed_component in nsx_edge['routed_components']:
			if 'OPS' in routed_component['name'].upper():
				opsmgr_routed_component = routed_component
			elif 'ERT' in routed_component['name'].upper():
				ert_routed_component = routed_component
			elif 'DIEGO' in routed_component['name'].upper():
				diego_routed_component = routed_component
			elif 'TCP' in routed_component['name'].upper():
				tcp_routed_component = routed_component

		ertLogicalSwitch = {}
		infraLogicalSwitch = {}

		for name, lswitch in nsx_edge['global_switches'].iteritems():
			if 'ERT' in name.upper():
				ertLogicalSwitch = lswitch
			elif 'INFRA' in name.upper():
				infraLogicalSwitch = lswitch
				

		vcenter_ctx = context['vcenter']
		nsx_edge['datacenter_id'] = mobclient.lookupMoid(vcenter_ctx['datacenter']) 

		# Use the cluster name/id for resource pool...
		#nsx_edge['host_id'] = mobclient.lookupMoid(vcenterMobMap, vcenter_ctx['host'])
		nsx_edge['datastore_id'] = mobclient.lookupMoid(vcenter_ctx['datastore']) 
		nsx_edge['cluster_id'] = mobclient.lookupMoid(vcenter_ctx['cluster'])   
		nsx_edge['resourcePool_id'] = mobclient.lookupMoid(vcenter_ctx['cluster'])
		
		# TODO: Ignore the vm folder for now...
		#nsx_edge['vmFolder_id'] = mobclient.lookupMoid(vcenter_ctx['vmFolder']) 


		# Get a large cidr (like 16) that would allow all networks to talk to each other
		infraIpSegment = infraLogicalSwitch['cidr'].split('/')[0]
		infraIpTokens  = infraIpSegment.split('.')
		cross_network_cidr = '{}.{}.0.0/16'.format(infraIpTokens[0], 
													infraIpTokens[1])

		gateway_address = nsx_edge.get('gateway_ip')
		if not gateway_address:
			global_uplink_ip = context['nsxmanager']['uplink_details']['uplink_ip']
			uplinkIpTokens   = global_uplink_ip.split('.')
			gateway_address  = '{}.{}.{}.1'.format(	uplinkIpTokens[0], 
													uplinkIpTokens[1],
													uplinkIpTokens[2])
		
		generate_certs(nsx_edge)
		if DEBUG:
			print 'NSX Edge config: {}\n'.format(str(nsx_edge))   

		nsx_edges_context = {
			'context': context,
			'defaults': context['defaults'],
			'nsxmanager': context['nsxmanager'],
			'edge': nsx_edge,
			'logical_switches': logical_switches,
			'empty_logical_switches': empty_logical_switches,
			'global_switches': nsx_edge['global_switches'],
			'appProfile_Map': nsx_edge['appProfile_Map'],
			'appRule_Map': nsx_edge['appRule_Map'],
			'infraLogicalSwitch': infraLogicalSwitch,
			'ertLogicalSwitch': ertLogicalSwitch,
			'routed_components':  nsx_edge['routed_components'],
			'opsmgr_routed_component': opsmgr_routed_component,
			'ert_routed_component': ert_routed_component,
			'diego_routed_component': diego_routed_component,
			'tcp_routed_component': tcp_routed_component,
			'cross_network_cidr': cross_network_cidr,
			'gateway_address': gateway_address,
			'files': []
		}    

		template.render(
			os.path.join(nsx_edges_dir, nsx_edge['name'] + '_post_payload.xml'),
			os.path.join(template_dir, 'edge_config_post_payload.xml' ),
			nsx_edges_context
		)

		
		post_response = client.post_xml(NSX_URLS['esg']['all'] , 
								os.path.join(nsx_edges_dir, nsx_edge['name'] + '_post_payload.xml'), 
								check=False)
		data = post_response.text

		print 'NSX Edge creation response:{}\n'.format(data)

		if post_response.status_code < 400: 
			print 'Created NSX Edge :{}\n'.format(nsx_edge['name'])
			certId = add_certs_to_nsx_edge(nsx_edges_dir, nsx_edge)
			print 'Got cert id: {}\n'.format(nsx_edge['cert_id'])
			print 'Now updating LBR config!!'
			add_lbr_to_nsx_edge(nsx_edges_dir, nsx_edge)
			print 'Success!! Finished creation of NSX Edge instance: {}\n\n'.format(nsx_edge['name'])
		else:
			print 'Creation of NSX Edge failed, details:{}\n'.format(data)			

def add_certs_to_nsx_edge(nsx_edges_dir, nsx_edge):
	map_nsx_esg_id( [ nsx_edge ] )

	template_dir = '.'
	nsx_edges_context = {
		'nsx_edge': nsx_edge,
		'files': []
	}    

	template.render(
		os.path.join(nsx_edges_dir, nsx_edge['name'] + '_cert_post_payload.xml'),
		os.path.join(template_dir, 'edge_cert_post_payload.xml' ),
		nsx_edges_context
	)

	retry = True
	while (retry):
			
		retry = False
		post_response = client.post_xml(NSX_URLS['cert']['all'] + '/' + nsx_edge['id'], 
				os.path.join(nsx_edges_dir, nsx_edge['name'] + '_cert_post_payload.xml'), check=False)
		data = post_response.text

		if DEBUG:
			print 'NSX Edge Cert Addition response:\{}\n'.format(data)

		if post_response.status_code < 400: 
			print 'Added NSX Edge Cert to {}\n'.format(nsx_edge['name'])
			certPostResponseDoc = xmltodict.parse(data)

			certId = certPostResponseDoc['certificates']['certificate']['objectId']
			nsx_edge['cert_id'] = certId

		print 'Addition of NSX Edge Cert failed, details:{}\n'.format(data)
		if post_response.status_code == 404: 
			print 'NSX Edge not yet up, retrying'
			retry = True 
			print 'Going to retry addition of cert again... for NSX Edge: {}\n'.format(nsx_edge['name'])


def add_lbr_to_nsx_edge(nsx_edges_dir, nsx_edge):
	
	template_dir = '.'
	
	nsx_edges_context = {
		'nsx_edge': nsx_edge,
		'appProfile_Map': nsx_edge['appProfile_Map'],
		'appRule_Map': nsx_edge['appRule_Map'],
		'monitor_Map': nsx_edge['monitor_Map'],
		'routed_components':  nsx_edge['routed_components'],
		'files': []
	}    

	template.render(
		os.path.join(nsx_edges_dir, nsx_edge['name'] + '_lbr_config_put_payload.xml'),
		os.path.join(template_dir, 'edge_lbr_config_put_payload.xml' ),
		nsx_edges_context
	)
	
	put_response = client.put_xml(NSX_URLS['esg']['all'] 
									+ '/' + nsx_edge['id']
									+ NSX_URLS['lbrConfig']['all'], 
									os.path.join(nsx_edges_dir, nsx_edge['name'] 
									+ '_lbr_config_put_payload.xml'), 
							check=False)
	data = put_response.text

	if DEBUG:
		print 'NSX Edge LBR Config Update response:{}\n'.format(data)

	if put_response.status_code < 400: 
		print 'Updated NSX Edge LBR Config for : {}\n'.format(nsx_edge['name'])		
	else:
		print 'Update of NSX Edge LBR Config failed, details:{}\n'.format(data)


def map_nsx_esg_id(edge_service_gateways):
	existingEsgResponse = client.get('/api/4.0/edges')
	existingEsgResponseDoc = xmltodict.parse(existingEsgResponse.text)

	matched_nsx_esgs = 0
	num_nsx_esgs = len(edge_service_gateways)
	
	if DEBUG:
		print 'ESG response :\n{}\n'.format(existingEsgResponse.text) 

	edgeSummaries = existingEsgResponseDoc['pagedEdgeList']['edgePage']['edgeSummary']
	edgeEntries = edgeSummaries
	if isinstance(edgeSummaries, dict):
		edgeEntries = [ edgeSummaries ]

	for existingEsg in edgeEntries:
		if (num_nsx_esgs == matched_nsx_esgs):
			break

		for interested_Esg in  edge_service_gateways: 
			if (interested_Esg['name'] == existingEsg['name'] ):
				interested_Esg['id'] = existingEsg['objectId']
				++matched_nsx_esgs
				break

	for interested_Esg in  edge_service_gateways: 
		if (interested_Esg.get('id') is None):
			print 'NSX ESG instance with name: {} does not exist anymore\n'.format(interested_Esg['name'])

	return matched_nsx_esgs   

def list_nsx_edge_gateways(context):
	existingEsgResponse = client.get(NSX_URLS['esg']['all'] )
	existingEsgResponseDoc = xmltodict.parse(existingEsgResponse.text)

	if DEBUG:
		print 'NSX ESG response :{}\n'.format(existingEsgResponse.text)

	edgeSummaries = existingEsgResponseDoc['pagedEdgeList']['edgePage']['edgeSummary']
	edgeEntries = edgeSummaries
	if isinstance(edgeSummaries, dict):
		edgeEntries = [ edgeSummaries ]

	"""
	for nsx_esg in edgeEntries:
		print '\nNSX Edge Service Gateway Instance:{}\n'.format(str(nsx_esg))
	"""
	print_edge_service_gateways_available(edgeEntries)
	
def delete_nsx_edge_gateways(context):

	edge_service_gateways = context['edge_service_gateways']
	map_nsx_esg_id(edge_service_gateways)
	
	for nsx_esg in edge_service_gateways:

		if  nsx_esg.get('id') is None:
			continue

		delete_response = client.delete(NSX_URLS['esg']['all'] + '/' +
				nsx_esg['id'])
		data = delete_response.text

		if DEBUG:
			print 'NSX ESG Deletion response:{}\n'.format(data)

		if delete_response.status_code < 400: 
			print 'Deleted NSX ESG : {}\n'.format(nsx_esg['name'])
		else:
			print 'Deletion of NSX ESG failed, details:{}\n'.format(data +'\n')    


def generate_certs(nsx_context):
	output_dir = './' + nsx_context['certs']['name']
	cert_config = nsx_context['certs']['config']
 
	org_unit = cert_config['org_unit']
	country = cert_config['country_code']

	subprocess.call([ './certGen.sh', cert_config['system_domain'], cert_config['app_domain'], output_dir, org_unit, country ])
	nsx_context['certs']['key'] = readFileContent(output_dir + '/*.key')
	nsx_context['certs']['cert'] = readFileContent(output_dir + '/*.crt')
	nsx_context['certs']['name'] = 'Cert for ' + nsx_context['certs']['name']
	nsx_context['certs']['description'] = nsx_context['certs']['name'] + '-' + nsx_context['certs']['config']['system_domain']
	#print 'Generated key:\n' + nsx_context['certs']['key']
	#print 'Generated cert:\n' + nsx_context['certs']['cert']

def readFileContent(filePath):
	txt = glob.glob(filePath)
	for textfile in txt:
		with open(textfile, 'r') as myfile:
			response=myfile.read()
		return response