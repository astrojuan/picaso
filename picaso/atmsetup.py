from .elements import ELEMENTS as ele 
import json 
import os
from .io_utils import read_json
import astropy.units as u
import astropy.constants as c
import pandas as pd
import warnings 
import numpy as np
from .wavelength import get_cld_input_grid, regrid
from numba import jit
import h5py 
import pysynphot as psyn

__refdata__ = os.environ.get('picaso_refdata')

class ATMSETUP():
	"""
	Reads in default source configuration from JSON and creates a full atmosphere class. 

	- Gets PT profile 
	- Computes mean molecular weight, density, column density, mixing ratios 
	- Gets cloud profile 
	- Gets stellar profile 

	"""
	def __init__(self, config):
		if __refdata__ is None:
			warnings.warn("Reference data has not been initialized, some functionality will be crippled", UserWarning)
		else: 
			self.ref_dir = os.path.join(__refdata__)
		self.input = config
		self.warnings = []
		self.planet = type('planet', (object,),{})
		self.layer = {}
		self.level = {}
		self.get_constants()

	def get_constants(self):
		"""
		This function gets all conversion factors based on input units and all 
		constants. Goal here is to get everything to cgs and make sure we are only 
		call lengths of things once (e.g. number of wavelength points, number of layers etc)

		Some stuff will be added to this in other routines (e.g. number of layers)
		"""
		self.c = type('c', (object,),{})
		#conversion units
		self.c.pconv = 1e6 #convert bars to cgs 

		#physical constants
		self.c.k_b = (c.k_B.to(u.erg / u.K).value) 		
		self.c.G = c.G.to(u.cm*u.cm*u.cm/u.g/u.s/u.s).value 
		self.c.amu = c.u.to(u.g).value #grams
		self.c.rgas = c.R.value
		self.c.pi = np.pi

		#code constants
		self.c.ngangle = self.input['disco']['num_gangle']
		self.c.ntangle = self.input['disco']['num_tangle']

		return

	def get_profile_3d(self):
		"""
		A separate routine is written to get the 3d profile because the inputs are much more 
		rigid. In this framework, the following restrictions are placed: 
		
		Warning
		-------
		The input must be in hdf5 format. The tutorial for 3d calculations 
		can help guid you to make these files correctly. 

		"""
		#SET DIMENSIONALITY 
		self.dimension = '3d'

		chemistry_input = self.input['atmosphere']
		h5db = h5py.File(chemistry_input['profile']['filepath'],'r+')
		#get header of columns
		header = h5db.attrs['header'].split(',')

		iheader = list(range(len(header)))

		#get all the gangles 
		self.gangles = [i for i in h5db.keys()]
		ng = self.c.ngangle
		#get all the tangles 
		self.tangles = [i for i in h5db[self.gangles[0]].keys()]
		nt = self.c.ntangle

		nlevel = h5db[str(self.gangles[0])][str(self.tangles[0])].shape[0]
		self.c.nlevel = nlevel
		self.c.nlayer = nlevel-1

		#fill empty arrays for everything
		self.level['temperature'] = np.zeros((nlevel, ng, nt))
		self.level['pressure'] = np.zeros((nlevel, ng, nt))
		self.layer['temperature'] = np.zeros((nlevel-1, ng, nt))
		self.layer['pressure'] = np.zeros((nlevel-1, ng, nt))

		if 'e-' in header:

			electron=True
			#check to see if there is an electron
			self.level['electrons'] = np.zeros((nlevel, ng, nt))
			self.layer['electrons'] = np.zeros((nlevel-1, ng, nt))
			ielec = np.where('e-' == np.array(header))[0][0]
		else: 
			electron=False		

		first = True
		#loop over gangles 
		for g in range(self.c.ngangle):
			#loop over tangles 
			for t in range(self.c.ntangle):
				if first:
					#on first pass find index where temperature and pressure are located
					itemp = np.where('temperature' == np.array(header))[0][0]
					ipress = np.where('pressure' == np.array(header))[0][0]
					iheader.pop(ielec)
					iheader.pop(itemp)
					iheader.pop(ipress)

					#after getting rid of electrons, temperature and pressure the rest should 
					#represent the number of molecules 
					num_mol = len(iheader)
					self.level['mixingratios'] = np.zeros((nlevel, num_mol, ng, nt))
					self.layer['mixingratios'] = np.zeros((nlevel-1, num_mol, ng, nt))

					self.weights = pd.DataFrame({})
					self.molecules=[]
					for i in iheader:

						self.weights[header[i]] = pd.Series([self.get_weights([header[i]])[header[i]]])
						self.molecules += [header[i]]

					first = False

				if electron: 
					#if there is an electron column, fill in the values 
					self.level['electrons'][:,g,t] = h5db[self.gangles[g]][self.tangles[t]].value[:,ielec]
					self.layer['electrons'][:,g,t] = 0.5*(self.level['electrons'][1:,g,t] + self.level['electrons'][:-1,g,t])

				self.level['temperature'][:,g,t] = h5db[self.gangles[g]][self.tangles[t]].value[:,itemp]
				self.layer['temperature'][:,g,t] = 0.5*(self.level['temperature'][1:,g,t] + self.level['temperature'][:-1,g,t])

				self.level['pressure'][:,g,t] = h5db[self.gangles[g]][self.tangles[t]].value[:,ipress]*self.c.pconv #CONVERTING BARS TO DYN/CM2
				self.layer['pressure'][:,g,t] = np.sqrt(self.level['pressure'][1:,g,t] * self.level['pressure'][:-1,g,t])


				self.level['mixingratios'][:,:,g,t] = h5db[self.gangles[g]][self.tangles[t]].value[:,iheader]
				self.layer['mixingratios'][:,:,g,t] = 0.5*(self.level['mixingratios'][1:,:,g,t] + self.level['mixingratios'][:-1,:,g,t])


	def get_profile(self):
		"""
		Get profile from file or from user input file or direct pandas dataframe. If PT profile 
		is not given, use parameterization 

		Currently only needs inputs from config file

		Todo
		----
		- Add regridding to this by having users be able to set a different nlevel than the input cloud code is
		"""
		#get chemistry input from configuration
		#SET DIMENSIONALITY
		self.dimension = '1d'

		chemistry_input = self.input['atmosphere']
		if chemistry_input['profile']['type'] == 'user':

			if chemistry_input['profile']['filepath'] != None:

				read = pd.read_csv(chemistry_input['profile']['filepath'], delim_whitespace=True)

			elif (isinstance(chemistry_input['profile']['profile'],pd.core.frame.DataFrame) or 
					isinstance(chemistry_input['profile']['profile'], dict)): 
					read = chemistry_input['profile']['profile']
			else:
				raise Exception("Provide dictionary or a pointer to pointer to filepath")
		else: 
			raise Exception("TODO: only capability for user is included right now")

		#if a subset is not specified, 
		#determine which of the columns is a molecule by trying to get it's weight 
		
		#COMPUTE THE MOLECULAT WEIGHTS OF THE MOLECULES
		weights = pd.DataFrame({})

		#users have the option of specifying a subset of specified file or pd dataframe
		if isinstance(chemistry_input['molecules']['whichones'], list):
			#make sure that the whichones list has more than one molecule
			num_mol = len(chemistry_input['molecules']['whichones'])
			self.molecules = np.array(chemistry_input['molecules']['whichones'])
			if num_mol >= 1:
				#go through list and compute molecular weights for each one
				for i in chemistry_input['molecules']['whichones']:
					#try to get the mmw of the molecule
					try:
						weights[i] = pd.Series([self.get_weights([i])[i]])
					except:
						#if there is an error, means its not a molecule
						if i == 'e-':
							#check to see if its an electron
							self.level['electrons'] = read['e-'].values
							self.layer['electrons'] = 0.5*(self.level['electrons'][1:] + self.level['electrons'][:-1])
						else:
							#if its not user messed up.. raise exception
							raise Exception("Molecule %s in whichones is not recognized, check list and resubmit" %i)
		else:
			#if one big file was uploaded, then cycle through each column
			self.molecules = np.array([],dtype=str)
			for i in read.keys():
				if i in ['pressure', 'temperature']: continue
				try:
					weights[i] = pd.Series([self.get_weights([i])[i]])
					self.molecules = np.concatenate((self.molecules ,np.array([i])))
				except:
					if i == 'e-':
						self.level['electrons'] = read['e-'].values
						self.layer['electrons'] = 0.5*(self.level['electrons'][1:] + self.level['electrons'][:-1])
					else:					#don't raise exception, instead add user warning that a column has been automatically skipped
						self.add_warnings("Ignoring %s in input file, not recognized molecule" % i)
			

		#DEFINE MIXING RATIOS
		self.level['mixingratios'] = read[list(weights.keys())].as_matrix()
		self.layer['mixingratios'] = 0.5*(self.level['mixingratios'][1:,:] + self.level['mixingratios'][:-1,:])
		self.weights = weights

		#GET TP PROFILE 
		#define these to see if they are floats check to see that they are floats 
		T = chemistry_input['PT']['T']
		logg1 = chemistry_input['PT']['logg1']
		logKir = chemistry_input['PT']['logKir']
		logPc = chemistry_input['PT']['logPc']

		#from file
		if ('temperature' in read.keys()) and ('pressure' in read.keys()):
			self.level['temperature'] = read['temperature'].values
			self.level['pressure'] = read['pressure'].values*self.c.pconv #CONVERTING BARS TO DYN/CM2
			self.layer['temperature'] = 0.5*(self.level['temperature'][1:] + self.level['temperature'][:-1])
			self.layer['pressure'] = np.sqrt(self.level['pressure'][1:] * self.level['pressure'][:-1])
		#from parameterization
		elif (isinstance(T,(float,int)) and isinstance(logg1,(float,int)) and 
							isinstance(logKir,(float,int)) and isinstance(logPc,(float,int))): 
			self.profile = calc_TP(T,logKir, logg1, logPc)
		#no other options supported so raise error 
		else:
			raise Exception("There is not adequte information to compute PT profile")

		#Define nlevel and nlayers after profile has been built
		self.c.nlevel = self.level['mixingratios'].shape[0]

		self.c.nlayer = self.c.nlevel - 1		

	def calc_PT(self, T, logKir, logg1, logPc):
		"""
		Calculates parameterized PT profile from Guillot. This isntance is here 
		primary for the retrieval scheme, so this can be updated

		Parameters
		----------

		T : float 
		logKir : float
		logg1 : float 
		logPc : float 
		"""
		raise Exception('TODO: Temperature parameterization option not included yet')
		return pd.DataFrame({'temperature':[], 'pressure':[], 'den':[], 'mu':[]})

	def get_needed_continuum(self):
		"""
		This will define which molecules are needed for the continuum opacities. THis is based on 
		temperature and molecules. Eventually CIA's will expand but we may not necessarily 
		want all of them. 
		'wno','h2h2','h2he','h2h','h2ch4','h2n2']

		Todo
		----
		- Add in temperature dependent to negate h- and things when not necessary
		"""
		self.continuum_molecules = []
		if "H2" in self.molecules:
			self.continuum_molecules += [['H2','H2']]
		if ("H2" in self.molecules) and ("He" in self.molecules):
			self.continuum_molecules += [['H2','He']]
		if ("H2" in self.molecules) and ("N2" in self.molecules):
			self.continuum_molecules += [['H2','N2']]	
		if 	("H2" in self.molecules) and ("H" in self.molecules):
			self.continuum_molecules += [['H2','H']]
		if 	("H2" in self.molecules) and ("CH4" in self.molecules):
			self.continuum_molecules += [['H2','CH4']]
		if ("H-" in self.molecules):
			self.continuum_molecules += [['H-','bf']]
		if ("H" in self.molecules) and ("electrons" in self.level.keys()):
			self.continuum_molecules += [['H-','ff']]
		if ("H2" in self.molecules) and ("electrons" in self.level.keys()):
			self.continuum_molecules += [['H2','H2-']]
		#now we can remove continuum molecules from self.molecules to keep them separate 
		if 'H+' in ['H','H2-','H2','H-','He','N2']: self.add_warnings('No H+ continuum opacity included')

		self.molecules = np.array([ x for x in self.molecules if x not in ['H','H2-','H2','H-','He','N2', 'H+'] ])

	def get_weights(self, molecule):
		"""
		Automatically gets mean molecular weights of any molecule. Requires that 
		user inputs case sensitive molecules i.e. TiO instead of TIO. 

		Parameters
		----------
		molecule : str or list
			any molecule string e.g. "H2O", "CH4" etc "TiO" or ["H2O", 'CH4']

		Returns
		-------
		dict 
			molecule as keys with molecular weights as value
		"""
		weights = {}
		if not isinstance(molecule,list):
			molecule = [molecule]

		for i in molecule:
			molecule_list = []
			for j in range(0,len(i)):
				try:
					molecule_list += [float(i[j])]
				except: 
					if i[j].isupper(): molecule_list += [i[j]] 
					elif i[j].islower(): molecule_list[j-1] =  molecule_list[j-1] + i[j]
			totmass=0
			for j in range(0,len(molecule_list)): 
				
				if isinstance(molecule_list[j],str):
					elem = ele[molecule_list[j]]
					try:
						num = float(molecule_list[j+1])
					except: 
						num = 1 
					totmass += elem.mass * num
			weights[i] = totmass
		return weights


	#@jit(nopython=True)
	def get_mmw(self):
		"""
		Returns the mean molecular weight of the atmosphere 
		"""
		if self.dimension=='1d':
			weighted_matrix = self.level['mixingratios'] @ self.weights.values[0]
		elif self.dimension=='3d':
			weighted_matrix=np.zeros((self.c.nlevel, self.c.ngangle, self.c.ntangle))
			for g in range(self.c.ngangle):
				for t in range(self.c.ntangle):
					weighted_matrix[:,g,t] = self.level['mixingratios'][:,:,g,t] @ self.weights.values[0]

		#levels are the edges
		self.level['mmw'] = weighted_matrix
		#layer is the midpoint
		self.layer['mmw'] = 0.5*(weighted_matrix[:-1]+weighted_matrix[1:])
		return 

	#@jit(nopython=True)
	def get_density(self):
		"""
		Calculates density of atmospheres used on TP profile: LEVEL
		"""
		self.level['den'] = self.level['pressure'] / (self.c.k_b * self.level['temperature']) 
		return

	#@jit(nopython=True)
	def get_column_density(self):
		"""
		Calculates the column desntiy based on TP profile: LAYER
		"""
		self.layer['colden'] = (self.level['pressure'][1:] - self.level['pressure'][:-1] ) / self.planet.gravity
		return

	def get_gravity(self):
		"""
		Get gravity based on mass and radius, or gravity inputs 
		"""
		planet_input = self.input['planet']
		if planet_input['gravity'] is not None:
			g = (planet_input['gravity']*u.Unit(planet_input['gravity_unit'])).to('cm/(s**2)')
			g = g.value
		elif (planet_input['mass'] is not None) and (planet_input['radius'] is not None):
			m = (planet_input['mass']*u.Unit(planet_input['mass_unit'])).to(u.g)
			r = ((planet_input['radius']*u.Unit(planet_input['radius_unit'])).to(u.cm))
			g = (self.c.G * m /  (r**2)).value
		else: 
			raise Exception('Need to specify gravity or radius and mass + additional units')
		self.planet.gravity = g 
		return


	def get_clouds(self, wno):
		"""
		Get cloud properties from .cld input returned from eddysed. The eddysed file should have the following specifications 

		1) Have the following column names (any order)  opd g0 w0 

		2) Be white space delimeted 

		3) Has to have values for pressure levels (N) and wavelengths (M). The row order should go:

		level wave opd w0 g0
		1.	 1.   ... . .
		1.	 2.   ... . .
		1.	 3.   ... . .
		.	  .	... . .
		.	  .	... . .
		1.	 M.   ... . .
		2.	 1.   ... . .
		.	  .	... . .
		N.	 .	... . .

		Warning
		-------
		The order of the rows is very important because each column will be transformed 
		into a matrix that has the size: [nlayer,nwave]. 

		Parameters
		----------
		wno : array
			Array in ascending order of wavenumbers. This is used to regrid the cloud output

		Todo 
		----
		- Allow users to add different kinds of "simple" cloud options like "isotropic scattering" or grey 
		opacity at certain pressure. 
		"""
		self.input_wno = get_cld_input_grid(self.input['atmosphere']['clouds']['wavenumber'])

		self.c.output_npts_wave = np.size(wno)
		self.c.input_npts_wave = len(self.input_wno)
		#if a cloud filepath exists... 
		if (self.input['atmosphere']['clouds']['filepath'] != None) and (self.dimension=='1d'):

			if os.path.exists(self.input['atmosphere']['clouds']['filepath']):	
				#read in the file that was supplied 		
				cld_input = pd.read_csv(self.input['atmosphere']['clouds']['filepath'], delim_whitespace = True) 
				#make sure cloud input has the correct number of waves and PT points and names
				assert cld_input.shape[0] == self.c.nlayer*self.c.input_npts_wave, "Cloud input file is not on the same grid as the input PT profile:"
				assert 'g0' in cld_input.keys(), "Please make sure g0 is a named column in cld file"
				assert 'w0' in cld_input.keys(), "Please make sure w0 is a named column in cld file"
				assert 'opd' in cld_input.keys(), "Please make sure opd is a named column in cld file"

				#then reshape and regrid inputs to be a nice matrix that is nlayer by nwave

				#total extinction optical depth 
				opd = np.reshape(cld_input['opd'].values, (self.c.nlayer,self.c.input_npts_wave))
				opd = regrid(opd, self.input_wno, wno)
				self.layer['cloud'] = {'opd': opd}
				#cloud assymetry parameter
				g0 = np.reshape(cld_input['g0'].values, (self.c.nlayer,self.c.input_npts_wave))
				g0 = regrid(g0, self.input_wno, wno)
				self.layer['cloud']['g0'] = g0
				#cloud single scattering albedo 
				w0 = np.reshape(cld_input['w0'].values, (self.c.nlayer,self.c.input_npts_wave))
				w0 = regrid(w0, self.input_wno, wno)
				self.layer['cloud']['w0'] = w0  

			else: 

				#raise an exception if the file doesnt exist 
				raise Exception('Cld file specified does not exist. Replace with None or find real file') 

		#if no filepath was given and nothing was given for g0/w0, then assume the run is cloud free and give zeros for all thi stuff		  
		elif (self.input['atmosphere']['clouds']['filepath'] == None) and (self.input['atmosphere']['scattering']['g0'] == None) and (self.dimension=='1d'):

			zeros = np.zeros((self.c.nlayer,self.c.output_npts_wave))
			self.layer['cloud'] = {'w0': zeros}
			self.layer['cloud']['g0'] = zeros
			self.layer['cloud']['opd'] = zeros

		#if a value for those are given add those to "scattering"
		#note there is a distinction here between the "cloud" single scattering albedo, asym factor, opacity and the "TOTAL"
		#single scattering albedo, and asym factor. This is why there are two separate entries for total and cloud

		elif (self.input['atmosphere']['clouds']['filepath'] == None) and (self.input['atmosphere']['scattering']['g0'] != None) and (self.dimension=='1d'):

			zeros = np.zeros((self.c.nlayer,self.c.output_npts_wave))
			#scattering is the TOTAL asym factor and single scattering albedo 
			self.layer['scattering'] = {'w0': zeros+self.input['atmosphere']['scattering']['w0']}
			self.layer['scattering']['g0'] = zeros+self.input['atmosphere']['scattering']['g0']
			#w0 and g0 here are the single scattering and asymm for the cloud ONLY
			self.layer['cloud'] = {'w0': zeros}
			self.layer['cloud']['g0'] = zeros
			self.layer['cloud']['opd'] = zeros

		#ONLY OPTION FOR 3D INPUT
		elif self.dimension=='3d':
			cld_input = h5py.File(self.input['atmosphere']['clouds']['filepath'])
			opd = np.zeros((self.c.nlayer,self.c.output_npts_wave,self.c.ngangle,self.c.ntangle))
			g0 = np.zeros((self.c.nlayer,self.c.output_npts_wave,self.c.ngangle,self.c.ntangle)) 
			w0 = np.zeros((self.c.nlayer,self.c.output_npts_wave,self.c.ngangle,self.c.ntangle))
			header = cld_input.attrs['header'].split(',')

			assert 'g0' in header, "Please make sure g0 is a named column in hdf5 cld file"
			assert 'w0' in header, "Please make sure w0 is a named column in hdf5 cld file"
			assert 'opd' in header, "Please make sure opd is a named column in hdf5 cld file"

			iopd = np.where('opd' == np.array(header))[0][0]
			ig0 = np.where('g0' == np.array(header))[0][0]
			iw0 = np.where('w0' == np.array(header))[0][0]
			#stick in clouds that are gangle and tangle dependent 
			for g in range(self.c.ngangle):
				for t in range(self.c.ntangle):

					data = cld_input[self.gangles[g]][self.tangles[t]]

					#make sure cloud input has the correct number of waves and PT points
					assert data.shape[0] == self.c.nlayer*self.c.input_npts_wave, "Cloud input file is not on the same grid as the input PT/Angles profile:"

					#Then, reshape and regrid inputs to be a nice matrix that is nlayer by nwave

					#total extinction optical depth 
					opd_lowres = np.reshape(data[:,iopd], (self.c.nlayer,self.c.input_npts_wave))
					opd[:,:,g,t] = regrid(opd_lowres, self.input_wno, wno)
					self.layer['cloud'] = {'opd': opd}

					#cloud assymetry parameter
					g0_lowres = np.reshape(data[:,ig0], (self.c.nlayer,self.c.input_npts_wave))
					g0[:,:,g,t] = regrid(g0_lowres, self.input_wno, wno)
					self.layer['cloud']['g0'] = g0

					#cloud single scattering albedo 
					w0_lowres = np.reshape(data[:,iw0], (self.c.nlayer,self.c.input_npts_wave))
					w0[:,:,g,t] = regrid(w0_lowres, self.input_wno, wno)
					self.layer['cloud']['w0'] = w0 			

		else:

			raise Exception("CLD input not recognized. Either input a filepath, or input None")

		return


	def add_warnings(self, warn):
		"""
		Accumulate warnings from ATMSETUP
		"""
		self.warnings += [warn]
		return

	def get_stellar_spec(self,wno, database, temp, metal, logg ):
		"""
		Get the stellar spectrum using pysynphot and interpolate onto a much finer grid than the 
		planet grid. 

		Warning
		-------
		If the resolution of the stellar spectrum models increase 
		to higher resolution you could take out the lines that start with `fine_wno_star` and 
		`fine_flux-star` 

		Parameters
		----------
		wno : array 
			Array of the planet model output wavenumber grid 
		database : str 
			The database to pull stellar spectrum from. See documentation for pysynphot. 
		temp : float 
			Teff of the stellar model 
		metal : float 
			Metallicity of the stellar model 
		logg : float 
			Logg cgs of the stellar model

		Returns 
		-------
		wno, flux 
			Wavenumber and stellar flux in wavenumber and FLAM units 
		"""
		sp = psyn.Icat(database, temp, metal, logg)
		sp.convert("um")
		sp.convert('flam') 
		wno_star = 1e4/sp.wave[::-1] #convert to wave number and flip
		flux_star = sp.flux[::-1]	 #flip here to get correct order 	
		#now we need to make sure that the stellar grid is on a 3x finer resolution 
		#than the model. 
		max_shift = np.max(wno)+6000 #this 6000 is just the max raman shift we could have 
		min_shift = np.min(wno) -2000 #it is just to make sure we cut off the right wave ranges

		#do a fail safe to make sure that star is on a fine enough grid for planet case 
		fine_wno_star = np.linspace(min_shift, max_shift, len(wno)*5)
		fine_flux_star = np.interp(fine_wno_star,wno_star, flux_star)
		return wno_star, flux_star, fine_wno_star, fine_flux_star

	def get_surf_reflect(self,nwno):
		"""
		Gets the surface reflectivity from input
		"""
		self.surf_reflect = np.zeros(nwno)
		return

	def disect(self,g,t):
		"""
		This disects the 3d input to a 1d input which is a function of a single gangle and tangle. 
		This makes it possible for us to get the opacities at each facet before we go into the 
		flux calculation

		Parameters
		----------

		g : int 
			Gauss angle index 
		t : int 
			Tchebyshev angle index  
		"""
		self.level['temperature'] = self.level['temperature'][:,g,t]
		self.level['pressure'] = self.level['pressure'][:,g,t]
		self.layer['temperature'] = self.layer['temperature'][:,g,t]
		self.layer['pressure'] = self.layer['pressure'][:,g,t]

		self.layer['mmw'] = self.layer['mmw'][:,g,t]
		self.layer['mixingratios'] = self.layer['mixingratios'][:,:,g,t]
		self.layer['colden'] = self.layer['colden'][:,g,t]
		self.layer['electrons'] = self.layer['electrons'][:,g,t]

		self.layer['cloud']['opd'] = self.layer['cloud']['opd'][:,:,g,t]
		self.layer['cloud']['g0']= self.layer['cloud']['g0'][:,:,g,t]
		self.layer['cloud']['w0'] = self.layer['cloud']['w0'][:,:,g,t]
